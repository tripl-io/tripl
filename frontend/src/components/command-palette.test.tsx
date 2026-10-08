import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import type { AuthUser, Project } from '@/types'
import { MAIN_CONTENT_ID } from '@/components/landmarks'
import { ActiveProjectContext } from './active-project-context'
import { buildNavGroups } from '@/lib/navigation'
import { AuthContext, type AuthContextValue } from './auth-context'
import { BranchProvider } from './branch-context'
import { CommandPaletteProvider } from './command-palette'
import {
  COMMAND_PALETTE_TRIGGER_ATTR,
  useCommandPalette,
} from './command-palette-context'
import { at } from '@/test/at'

// Lets one test make the palette dialog fail the way a missing chunk does,
// while every other test gets the real dialog.
const paletteDialog = vi.hoisted(() => ({ fail: false }))
vi.mock('@/components/command-palette-dialog', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/components/command-palette-dialog')>()
  const Real = actual.default
  return {
    ...actual,
    default: (props: Parameters<typeof Real>[0]) => {
      if (paletteDialog.fail) {
        throw new TypeError('Failed to fetch dynamically imported module: /assets/palette-abc.js')
      }
      return <Real {...props} />
    },
  }
})

function mockJsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const ownerUser: AuthUser = {
  id: 'user-1',
  email: 'owner@example.com',
  name: 'Owner',
  role: 'owner',
  is_platform_admin: false,
  orgs: [],
  created_at: '2026-04-18T10:00:00Z',
  updated_at: '2026-04-18T10:00:00Z',
}

const authValue: AuthContextValue = {
  user: ownerUser,
  status: 'authenticated',
  error: null,
  isLoggingOut: false,
  logout: async () => {},
  refresh: () => {},
}

/**
 * The project payload the palette reads: identity, plus the summary the nav
 * model takes its badges from. Every counter is quiet — the palette renders no
 * counts, and a fixture that only ever carries zeros keeps the tests about
 * which DESTINATIONS are offered.
 */
function demoProject(overrides: { id?: string; name?: string; slug?: string } = {}) {
  return {
    id: 'project-1',
    name: 'Demo',
    slug: 'demo',
    ...overrides,
    description: '',
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
}

function PaletteOpener() {
  const palette = useCommandPalette()
  return (
    <button type="button" onClick={() => palette.setOpen(true)} data-testid="open-palette">
      open
    </button>
  )
}

/** Stands in for the top-bar search button that the palette falls back to. */
function TopBarTrigger() {
  const palette = useCommandPalette()
  return (
    <button
      type="button"
      {...{ [COMMAND_PALETTE_TRIGGER_ATTR]: '' }}
      onClick={() => palette.setOpen(true)}
      data-testid="topbar-trigger"
    >
      search
    </button>
  )
}

function LocationBeacon() {
  const location = useLocation()
  return (
    <>
      <div data-testid="location">{location.pathname}</div>
      {/* Separate from the pathname beacon so the many exact-path assertions
          above stay untouched; only the branch test reads the query. */}
      <div data-testid="location-search">{location.search}</div>
    </>
  )
}

function renderHarness(
  initialEntry = '/p/demo/events',
  auth: AuthContextValue = authValue,
  {
    // Opt-in: with a BranchProvider mounted the palette's requests carry
    // `?branch=`, which the exact-URL fetch mocks of the other tests reject.
    withBranchProvider = false,
  }: { withBranchProvider?: boolean } = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const eventsSurface = (
    <CommandPaletteProvider>
      <LocationBeacon />
      <PaletteOpener />
      <TopBarTrigger />
    </CommandPaletteProvider>
  )
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <MemoryRouter initialEntries={[initialEntry]}>
          <Routes>
            <Route
              path="/p/:slug/events"
              element={
                withBranchProvider ? (
                  <BranchProvider slug="demo">{eventsSurface}</BranchProvider>
                ) : (
                  eventsSurface
                )
              }
            />
            <Route
              path="/p/:slug/settings"
              element={
                <CommandPaletteProvider>
                  <LocationBeacon />
                </CommandPaletteProvider>
              }
            />
            <Route path="/p/:slug/events/detail/:eventId" element={<LocationBeacon />} />
            <Route path="/p/:slug/settings/:tab" element={<LocationBeacon />} />
            <Route path="/p/:slug/monitoring" element={<LocationBeacon />} />
            <Route path="/p/:slug/alerting" element={<LocationBeacon />} />
            <Route path="/p/:slug/branches" element={<LocationBeacon />} />
          </Routes>
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  paletteDialog.fail = false
  vi.restoreAllMocks()
  // BranchProvider persists the selection per slug; a branch left behind by
  // one test would silently attach `?branch=` to the next test's requests.
  window.localStorage.clear()
})

describe('CommandPalette', () => {
  it('opens via setOpen, lists projects, and navigates on select', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects')) {
        return mockJsonResponse([
          {
            id: 'project-1',
            name: 'Demo',
            slug: 'demo',
            description: '',
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
          },
        ])
      }
      if (url.endsWith('/api/v1/projects/demo/event-types')) {
        return mockJsonResponse([])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderHarness('/p/demo/events')

    fireEvent.click(screen.getByTestId('open-palette'))

    expect(await screen.findByPlaceholderText(/Search projects/i)).toBeInTheDocument()
    expect(await screen.findByText('Demo')).toBeInTheDocument()
    // The sidebar's words, not a palette-only dialect: these rows used to read
    // "Event type settings", "Meta field settings", "Relation settings",
    // "Variable settings" and "Monitoring settings".
    expect(screen.getByText('Event types')).toBeInTheDocument()
    // Renamed for what it holds (#238); the old name is a keyword.
    expect(screen.getByText('Meta fields')).toBeInTheDocument()
    expect(screen.getByText('Relations')).toBeInTheDocument()
    expect(screen.getByText('Properties')).toBeInTheDocument()
    expect(screen.getByText('Detection settings')).toBeInTheDocument()
    expect(screen.getByText('Alerting')).toBeInTheDocument()
    expect(screen.getByText('Scans')).toBeInTheDocument()

    // The keyboard-active row wears the focus ring, the same one the settings
    // palette's rows wear: cmdk keeps DOM focus in the input, so the ring
    // rides on aria-selected rather than on :focus-visible.
    const selected = await waitFor(() => {
      const row = screen.getAllByRole('option').find(option => option.getAttribute('aria-selected') === 'true')
      expect(row).toBeDefined()
      return row as HTMLElement
    })
    expect(selected).toHaveClass('aria-selected:ring-2', 'aria-selected:ring-inset', 'aria-selected:ring-ring')

    fireEvent.click(screen.getByText('Project settings'))

    await waitFor(() => {
      expect(screen.getByTestId('location').textContent).toBe('/p/demo/settings')
    })
  })

  it('navigates to project monitoring from the command palette', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects')) {
        return mockJsonResponse([
          {
            id: 'project-1',
            name: 'Demo',
            slug: 'demo',
            description: '',
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
          },
        ])
      }
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderHarness('/p/demo/events')

    fireEvent.click(screen.getByTestId('open-palette'))
    // Named as the page names itself. It is not a sidebar destination — the
    // Anomalies item claims the route by match — so it is listed by hand next to
    // the other off-model destinations rather than derived.
    fireEvent.click(await screen.findByText('Detection settings'))

    await waitFor(() => {
      expect(screen.getByTestId('location').textContent).toBe('/p/demo/settings/monitoring')
    })
  })

  it('labels navigation with the same terms as the sidebar and settings nav', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    // A platform admin, so the Platform group's sections are offered too.
    renderHarness('/p/demo/events', { ...authValue, user: { ...ownerUser, is_platform_admin: true } })

    fireEvent.click(screen.getByTestId('open-palette'))

    // Canonical terms shared with the settings nav.
    expect(await screen.findByText('Members')).toBeInTheDocument()
    expect(screen.getByText('Runtime')).toBeInTheDocument()
    expect(screen.getByText('Mail relay')).toBeInTheDocument()
    expect(screen.getByText('AI & search')).toBeInTheDocument()

    // The old, divergent palette-only labels are gone.
    expect(screen.queryByText('Users')).toBeNull()
    expect(screen.queryByText('Service settings')).toBeNull()
  })

  it('offers every sidebar destination, so the jump list cannot drift', async () => {
    // The hand-written list this replaces covered 8 of the sidebar's
    // destinations. Typing "anomalies" matched no row, so every static group
    // vanished and the query fell through to knowledge search — the user got
    // event types and fields back, never the page, and no signal that no such
    // page existed. Derived from `buildNavGroups`, so a nav item added tomorrow
    // is in the palette the same day.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects')) return mockJsonResponse([demoProject()])
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderHarness('/p/demo/events')
    fireEvent.click(screen.getByTestId('open-palette'))
    await screen.findByText('Demo')

    for (const item of buildNavGroups('demo', undefined).flatMap((group) => group.items)) {
      expect(screen.getByText(item.label), `no palette row for "${item.label}"`).toBeInTheDocument()
    }
    // Plus the destinations the sidebar renders outside the nav model.
    expect(screen.getByText('Project settings')).toBeInTheDocument()
    expect(screen.getByText('Concepts')).toBeInTheDocument()
  })

  it('does not offer a non-owner the owner-only destinations', async () => {
    // The sidebar drops `ownerOnly` items because the route behind them 403s;
    // a palette that mapped the nav model without the same filter would hand an
    // editor a row straight into that wall.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects')) return mockJsonResponse([demoProject()])
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderHarness('/p/demo/events', { ...authValue, user: { ...ownerUser, role: 'member' } })
    fireEvent.click(screen.getByTestId('open-palette'))
    await screen.findByText('Demo')

    expect(screen.queryByText('Audit log')).toBeNull()
    expect(screen.queryByText('Instance audit log')).toBeNull()
    // The organization's own settings are its owners' and admins'.
    expect(screen.queryByText('Email')).toBeNull()
    expect(screen.queryByText('AI')).toBeNull()
    expect(screen.queryByText('Limits')).toBeNull()
    // The Platform sections are the platform admin's alone.
    for (const label of ['Runtime', 'Mail relay', 'AI & search', 'Storage', 'Observability', 'System', 'Security & access']) {
      expect(screen.queryByText(label), `non-owner offered "${label}"`).toBeNull()
    }
    // Everything else still shows: this is a filter, not an empty nav.
    expect(screen.getByText('Anomalies')).toBeInTheDocument()
  })

  it('offers an organization owner its settings but not the Platform ones (F20 PR9)', async () => {
    // Platform sections carry only `platformOnly`: a palette filtering on
    // `ownerOnly` alone would hand every signed-in user the operator console.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects')) return mockJsonResponse([demoProject()])
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderHarness('/p/demo/events')
    fireEvent.click(screen.getByTestId('open-palette'))
    await screen.findByText('Demo')

    expect(screen.getByText('Email')).toBeInTheDocument()
    expect(screen.getByText('AI')).toBeInTheDocument()
    expect(screen.getByText('Limits')).toBeInTheDocument()
    expect(screen.getByText('Instance audit log')).toBeInTheDocument()
    for (const label of ['Runtime', 'Mail relay', 'AI & search', 'Storage', 'Observability', 'System', 'Security & access']) {
      expect(screen.queryByText(label), `owner offered "${label}"`).toBeNull()
    }
  })

  it('does not offer on a public demo the organization settings the demo refuses', async () => {
    // A public demo answers every change to the organization's own mail, AI,
    // search, photos, trackers and limits with 403; the settings rail does not
    // list them there, and the palette built from its model may not either.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/auth/status')) {
        return mockJsonResponse({ has_users: true, registration_enabled: false, public_demo: true })
      }
      if (url.endsWith('/api/v1/projects')) return mockJsonResponse([demoProject()])
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderHarness('/p/demo/events')
    fireEvent.click(screen.getByTestId('open-palette'))
    await screen.findByText('Demo')

    // The rest of the organization's settings stay.
    expect(screen.getByText('Invitations')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByText('Email')).toBeNull())
    for (const label of ['AI', 'Photos', 'Trackers', 'Limits']) {
      expect(screen.queryByText(label), `public demo offered "${label}"`).toBeNull()
    }
  })

  it('toggles via ⌘K keyboard shortcut', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderHarness('/p/demo/events')

    expect(screen.queryByPlaceholderText(/Search projects/i)).toBeNull()

    fireEvent.keyDown(window, { key: 'k', metaKey: true })

    expect(await screen.findByPlaceholderText(/Search projects/i)).toBeInTheDocument()
  })

  it('toggles via Ctrl+K on non-Mac platforms', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderHarness('/p/demo/events')

    expect(screen.queryByPlaceholderText(/Search projects/i)).toBeNull()

    // The keydown handler is intentionally permissive (Meta OR Ctrl) so the
    // shortcut works on Linux/Windows where there is no Meta key.
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true })

    expect(await screen.findByPlaceholderText(/Search projects/i)).toBeInTheDocument()
  })

  it('shows typed knowledge search results and navigates to their route', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects')) {
        return mockJsonResponse([
          {
            id: 'project-1',
            name: 'Demo',
            slug: 'demo',
            description: '',
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
          },
        ])
      }
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/search?')) {
        return mockJsonResponse({
          items: [
            {
              id: 'doc-1',
              entity_type: 'event',
              entity_id: 'event-1',
              parent_event_id: 'event-1',
              title: 'Checkout Completed',
              subtitle: 'Checkout',
              snippet: 'завершение покупки',
              route_path: '/p/demo/events/detail/event-1',
              score: 8,
              highlights: ['завершение покупки'],
              semantic_used: false,
            },
          ],
          total: 1,
          semantic_used: false,
        })
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderHarness('/p/demo/events')

    fireEvent.click(screen.getByTestId('open-palette'))
    fireEvent.change(await screen.findByPlaceholderText(/Search projects/i), {
      target: { value: 'покупки' },
    })

    fireEvent.click(await screen.findByText('Checkout Completed'))

    await waitFor(() => {
      expect(screen.getByTestId('location').textContent).toBe('/p/demo/events/detail/event-1')
    })
  })

  it('marks only semantically matched results with a semantic chip', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects')) {
        return mockJsonResponse([
          {
            id: 'project-1',
            name: 'Demo',
            slug: 'demo',
            description: '',
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
          },
        ])
      }
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/search?')) {
        return mockJsonResponse({
          items: [
            {
              id: 'doc-sem',
              entity_type: 'event',
              entity_id: 'event-1',
              parent_event_id: 'event-1',
              title: 'Refund Issued',
              subtitle: 'Billing',
              snippet: 'customer got their money back',
              route_path: '/p/demo/events/detail/event-1',
              score: 8,
              highlights: [],
              semantic_used: true,
            },
            {
              id: 'doc-lex',
              entity_type: 'event',
              entity_id: 'event-2',
              parent_event_id: 'event-2',
              title: 'Checkout Completed',
              subtitle: 'Checkout',
              snippet: 'money back guarantee shown',
              route_path: '/p/demo/events/detail/event-2',
              score: 6,
              highlights: [],
              semantic_used: false,
            },
          ],
          total: 2,
          semantic_used: true,
        })
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderHarness('/p/demo/events')

    fireEvent.click(screen.getByTestId('open-palette'))
    fireEvent.change(await screen.findByPlaceholderText(/Search projects/i), {
      target: { value: 'money back' },
    })

    expect(await screen.findByText('Refund Issued')).toBeInTheDocument()
    expect(screen.getByText('Checkout Completed')).toBeInTheDocument()
    // Exactly the semantic_used row carries the chip — the lexical one does not.
    expect(screen.getAllByText('semantic')).toHaveLength(1)
    // ...and what the chip CLAIMS is about the keyword ranking, not about the
    // keyword index. The lexical leg is a LIMIT-ed scan, so a
    // weak match displaced from its window re-enters through the meaning leg
    // and carries this chip while containing the word that was typed — "no
    // keyword matched this" would be false on exactly that row.
    const chip = screen.getByText('semantic')
    expect(chip).toHaveAttribute(
      'title',
      "Found by meaning — the keyword ranking didn't surface this",
    )
    expect(chip.getAttribute('title')).not.toMatch(/no keyword matched/i)
  })

  it('labels a nameless event result rather than rendering an empty row', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects')) return mockJsonResponse([demoProject()])
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/search?')) {
        return mockJsonResponse({
          items: [
            {
              id: 'doc-blank',
              entity_type: 'event',
              entity_id: 'event-1',
              parent_event_id: 'event-1',
              // The search document takes its title from `event.name`, so the
              // one acme-ios row with a blank name arrives here blank: the row
              // rendered an icon and nothing else.
              title: '',
              subtitle: 'Checkout',
              snippet: '',
              route_path: '/p/demo/events/detail/event-1',
              score: 8,
              highlights: [],
              semantic_used: false,
            },
            {
              id: 'doc-var',
              entity_type: 'variable',
              entity_id: 'var-1',
              parent_event_id: null,
              title: '${user_id}',
              subtitle: '',
              snippet: '',
              route_path: '/p/demo/variables',
              score: 4,
              highlights: [],
              semantic_used: false,
            },
          ],
          total: 2,
          truncated: false,
          semantic_used: false,
        })
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderHarness('/p/demo/events')

    fireEvent.click(screen.getByTestId('open-palette'))
    fireEvent.change(await screen.findByPlaceholderText(/Search projects/i), {
      target: { value: 'checkout' },
    })

    const blankRow = await screen.findByText('(unnamed event)')
    // The fallback is event-scoped on purpose: every other entity's title is
    // non-empty by schema, so a blanket placeholder there would be a lie.
    expect(screen.getByText('${user_id}')).toBeInTheDocument()

    // Labelled, and still a real row: selecting it is how the user reaches the
    // event in order to rename or archive it.
    fireEvent.click(blankRow)
    await waitFor(() => {
      expect(screen.getByTestId('location').textContent).toBe('/p/demo/events/detail/event-1')
    })
  })
})

/**
 * The palette used to hand every rendered row to cmdk's own fuzzy scorer, which
 * sorts AND re-appends the DOM nodes. These cover what turning that off has to
 * preserve, the one thing it exists to preserve, and the two list-empty states
 * it made reachable for the first time.
 */
describe('CommandPalette ranking and filtering', () => {
  /**
   * One row of a search response. `entityType` and `score` are parameters and
   * not constants on purpose, because both were constants once and both hid a
   * regression: with every fixture typed `event` only one group ever rendered,
   * so the first-appearance group ordering in `groupSearchResults` was untested
   * and a `.sort()` over the group list passed; with every fixture at `score: 8`
   * `Array#sort` was stable, so re-sorting the rows by score changed no DOM and
   * that passed too.
   */
  function searchDocument(fields: {
    id: string
    entityType: string
    title: string
    subtitle: string
    score: number
  }) {
    return {
      id: fields.id,
      entity_type: fields.entityType,
      entity_id: fields.id,
      parent_event_id: fields.id,
      title: fields.title,
      subtitle: fields.subtitle,
      snippet: '',
      route_path: `/p/demo/events/detail/${fields.id}`,
      score: fields.score,
      highlights: [],
      semantic_used: false,
    }
  }

  /** Mocks projects + event types for one project, and the search route with `search`. */
  function mockPalette(
    project: { id: string; name: string; slug: string },
    search: (url: string) => Promise<Response>,
  ) {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects')) {
        return mockJsonResponse([demoProject(project)])
      }
      if (url.endsWith(`/api/v1/projects/${project.slug}/event-types`)) {
        return mockJsonResponse([])
      }
      if (url.includes(`/api/v1/projects/${project.slug}/search?`)) return search(url)
      throw new Error(`Unhandled fetch: ${url}`)
    })
  }

  const emptySearch = async () =>
    mockJsonResponse({ items: [], total: 0, semantic_used: false })

  it('renders knowledge results in the order the API returned them', async () => {
    // Three rows chosen so that every plausible client-side reordering is
    // VISIBLE here, not just absent:
    //
    // * Two entity types, with rank 0 held by the non-`event` one. Ordering the
    //   headings by anything but arrival — say `SEARCH_TYPE_META`'s key order,
    //   where `event` comes first — moves the variable group below the events.
    // * Scores that CONTRADICT the server's order: the rank-0 row scores lowest
    //   and the last row scores highest. Any re-sort by `score`, whether over
    //   the whole list or within one bucket, inverts a pair the assertions pin.
    //   (The real service ships descending score; disagreeing here is the whole
    //   point — the palette must render what it was handed, not recompute it.)
    // * The two event rows are the pathological pair for cmdk's own scorer: it
    //   anchors on the start of the string, so it ranked the short literal title
    //   first (0.98990) and the stem-matched body hit second (0.89100) — the
    //   exact inversion the search service was written to avoid (#114).
    mockPalette({ id: 'project-1', name: 'Demo', slug: 'demo' }, async () =>
      mockJsonResponse({
        items: [
          searchDocument({
            id: 'doc-ranked-0',
            entityType: 'variable',
            title: 'Архивный порог',
            subtitle: 'archive_threshold',
            score: 3,
          }),
          searchDocument({
            id: 'doc-ranked-1',
            entityType: 'event',
            title: 'Пользователь открыл архив',
            subtitle: 'Архив',
            score: 4,
          }),
          searchDocument({
            id: 'doc-ranked-2',
            entityType: 'event',
            title: 'Архивы экрана',
            subtitle: 'Экран',
            score: 9,
          }),
        ],
        total: 3,
        semantic_used: false,
      }),
    )

    renderHarness('/p/demo/events')

    fireEvent.click(screen.getByTestId('open-palette'))
    fireEvent.change(await screen.findByPlaceholderText(/Search projects/i), {
      target: { value: 'архивы' },
    })

    await screen.findByText('Пользователь открыл архив')

    // Read the rendered rows, not the response: cmdk reorders by appendChild, so
    // only DOM position can tell whether the server's ranking survived. The
    // query is Cyrillic and every static label is not, so these options are the
    // knowledge rows and nothing else.
    const rows = screen.getAllByRole('option').map(row => row.textContent ?? '')
    const positionOf = (title: string) => rows.findIndex(text => text.includes(title))
    const weakestFirst = positionOf('Архивный порог')
    const stemMatched = positionOf('Пользователь открыл архив')
    const literalMatch = positionOf('Архивы экрана')

    expect(weakestFirst).toBe(0)
    // Group order: the variable arrived first, so its heading comes first.
    expect(stemMatched).toBeGreaterThan(weakestFirst)
    // Row order inside the event group: server order, not score order.
    expect(literalMatch).toBeGreaterThan(stemMatched)
  })

  it('narrows the static groups as you type', async () => {
    mockPalette({ id: 'project-1', name: 'Demo', slug: 'demo' }, emptySearch)

    renderHarness('/p/demo/events')

    fireEvent.click(screen.getByTestId('open-palette'))
    expect(await screen.findByText('All projects')).toBeInTheDocument()

    fireEvent.change(screen.getByPlaceholderText(/Search projects/i), {
      target: { value: 'anomalies' },
    })

    await waitFor(() => {
      expect(screen.queryByText('All projects')).toBeNull()
    })
    expect(screen.getByText('Anomalies')).toBeInTheDocument()
    expect(screen.queryByText('Data sources')).toBeNull()
  })

  it('drops a group heading when nothing in that group matches', async () => {
    mockPalette({ id: 'project-1', name: 'Demo', slug: 'demo' }, emptySearch)

    renderHarness('/p/demo/events')

    fireEvent.click(screen.getByTestId('open-palette'))
    expect(await screen.findByText('Projects')).toBeInTheDocument()

    fireEvent.change(screen.getByPlaceholderText(/Search projects/i), {
      target: { value: 'anomalies' },
    })

    // cmdk hides nothing once its own filter is off, so an empty group keeps its
    // heading on screen unless the group itself declines to render.
    await waitFor(() => {
      expect(screen.queryByText('Demo')).toBeNull()
    })
    expect(screen.queryByText('Projects')).toBeNull()
  })

  it('keeps a project findable by its slug', async () => {
    // Name and slug share no substring, so only a haystack holding both can find
    // this row — the term the deleted `keywords={[slug, name]}` used to carry.
    mockPalette(
      { id: 'project-1', name: 'Acme Analytics', slug: 'checkout-web' },
      emptySearch,
    )

    renderHarness('/p/checkout-web/events')

    fireEvent.click(screen.getByTestId('open-palette'))
    expect(await screen.findByText('Acme Analytics')).toBeInTheDocument()

    fireEvent.change(screen.getByPlaceholderText(/Search projects/i), {
      target: { value: 'checkout-web' },
    })

    await waitFor(() => {
      expect(screen.queryByText('All projects')).toBeNull()
    })
    expect(screen.getByText('Acme Analytics')).toBeInTheDocument()
  })

  it('does not claim "No matches." while the knowledge search is in flight', async () => {
    mockPalette(
      { id: 'project-1', name: 'Demo', slug: 'demo' },
      () => new Promise<Response>(() => {}),
    )

    renderHarness('/p/demo/events')

    fireEvent.click(screen.getByTestId('open-palette'))
    fireEvent.change(await screen.findByPlaceholderText(/Search projects/i), {
      // Matches no static label or route, so every static group is gone and the
      // registered-item count is 0 — the state cmdk reports as "empty".
      target: { value: 'zzzqqq' },
    })

    expect(await screen.findByText('Searching.')).toBeInTheDocument()
    expect(screen.queryByText('No matches.')).toBeNull()
  })

  it('claims nothing about the knowledge base during the debounce window', async () => {
    mockPalette({ id: 'project-1', name: 'Demo', slug: 'demo' }, emptySearch)

    renderHarness('/p/demo/events')

    fireEvent.click(screen.getByTestId('open-palette'))
    // Wait for the project list first: without a loaded project there is no slug
    // to search, and the palette would be quiet for a reason that has nothing to
    // do with the debounce.
    expect(await screen.findByText('Demo')).toBeInTheDocument()

    fireEvent.change(screen.getByPlaceholderText(/Search projects/i), {
      target: { value: 'zzzqqq' },
    })

    // Asserted synchronously, on purpose: this is the SEARCH_DEBOUNCE_MS window
    // before the request is even sent. The static rows narrow against the LIVE
    // query, so they are already gone; anything the list says here it says about
    // a search that has not run yet. A state machine keyed on the debounced
    // query alone reads this moment as "answered, and the answer is nothing".
    expect(screen.getByText('Searching.')).toBeInTheDocument()
    expect(screen.queryByText(/No results for/)).toBeNull()
    expect(screen.queryByText('No matches.')).toBeNull()
  })

  it('keeps the fetched rows on screen while the next search is in flight', async () => {
    // The query key carries the debounced text, so each 200ms boundary minted a
    // key with no data and the list fell back to a single "Searching." line for
    // the whole round trip — measured at over 2.2s, during which the dialog held
    // zero selectable rows while the events table behind it listed the very rows
    // being searched for. Narrowing beats blanking.
    // Every query is two requests — the keyword-only leg and the full one
    // — so the first query's pair answers and the second
    // query's pair never does.
    let searches = 0
    mockPalette({ id: 'project-1', name: 'Demo', slug: 'demo' }, async () => {
      searches += 1
      if (searches > 2) return new Promise<Response>(() => {})
      return mockJsonResponse({
        items: [
          searchDocument({
            id: 'doc-session',
            entityType: 'event',
            title: 'Session Started',
            subtitle: 'Session',
            score: 9,
          }),
        ],
        total: 1,
        semantic_used: false,
      })
    })

    renderHarness('/p/demo/events')

    fireEvent.click(screen.getByTestId('open-palette'))
    const input = await screen.findByPlaceholderText(/Search projects/i)
    fireEvent.change(input, { target: { value: 'session' } })
    expect(await screen.findByText('Session Started')).toBeInTheDocument()

    fireEvent.change(input, { target: { value: 'session_start' } })

    // Wait until the SECOND request is actually out, so this is the new query
    // key and not just the debounce window: that is the moment the rows used to
    // disappear.
    await waitFor(() => expect(searches).toBe(4))
    expect(screen.getByText('Session Started')).toBeInTheDocument()
    expect(screen.getByText('Updating results…')).toBeInTheDocument()
    // The cold-start line belongs to a palette session that has never had
    // results; it must not replace rows the reader is already looking at.
    expect(screen.queryByText('Searching.')).toBeNull()
  })

  it('does not hand the previous search’s rows to an unrelated one', async () => {
    // The palette is mounted for the life of the page — only the dialog unmounts
    // — so React Query's observer keeps the last rows it saw forever. Without a
    // record of which query they answer, "checkout"'s results came back under
    // "Updating results…" for every later search, dimmed but selectable: Enter
    // navigated to a checkout result while the input read something else.
    // Every query is two requests — the keyword-only leg and the full one
    // — so the first query's pair answers and the second
    // query's pair never does.
    let searches = 0
    mockPalette({ id: 'project-1', name: 'Demo', slug: 'demo' }, async () => {
      searches += 1
      if (searches > 2) return new Promise<Response>(() => {})
      return mockJsonResponse({
        items: [
          searchDocument({
            id: 'doc-session',
            entityType: 'event',
            title: 'Session Started',
            subtitle: 'Session',
            score: 9,
          }),
        ],
        total: 1,
        semantic_used: false,
      })
    })

    renderHarness('/p/demo/events')

    fireEvent.click(screen.getByTestId('open-palette'))
    const input = await screen.findByPlaceholderText(/Search projects/i)
    fireEvent.change(input, { target: { value: 'session' } })
    expect(await screen.findByText('Session Started')).toBeInTheDocument()

    // Clearing the input abandons that search; what follows is a new one.
    fireEvent.change(input, { target: { value: '' } })
    fireEvent.change(input, { target: { value: 'zzzqqq' } })

    await waitFor(() => expect(searches).toBe(4))
    expect(screen.queryByText('Session Started')).toBeNull()
    expect(screen.queryByText('Updating results…')).toBeNull()
    expect(screen.getByText('Searching.')).toBeInTheDocument()
  })

  it('shows "No matches." when a query below the search floor matches nothing', async () => {
    mockPalette({ id: 'project-1', name: 'Demo', slug: 'demo' }, emptySearch)

    renderHarness('/p/demo/events')

    fireEvent.click(screen.getByTestId('open-palette'))
    expect(await screen.findByText('All projects')).toBeInTheDocument()

    fireEvent.change(screen.getByPlaceholderText(/Search projects/i), {
      // One character, so knowledge search never starts (2-char floor), and one
      // that appears in no static label or route — the only shape of query for
      // which the palette has nothing whatsoever to show.
      target: { value: 'z' },
    })

    // The palette's only "we found you nothing" affordance. Asserted PRESENT,
    // because every other assertion about it in this file is an absence — so
    // deleting the block outright used to keep the suite green.
    expect(await screen.findByText('No matches.')).toBeInTheDocument()
    expect(screen.queryByText('All projects')).toBeNull()
    expect(screen.queryByText('Sign out')).toBeNull()
  })

  it('shows one empty state, not two, when the knowledge search comes back empty', async () => {
    mockPalette({ id: 'project-1', name: 'Demo', slug: 'demo' }, emptySearch)

    renderHarness('/p/demo/events')

    fireEvent.click(screen.getByTestId('open-palette'))
    fireEvent.change(await screen.findByPlaceholderText(/Search projects/i), {
      // Matches no static label or route, so every static group is gone: the
      // configuration where the list-wide empty line and the knowledge
      // section's own empty line both used to render, one above the other.
      target: { value: 'zzzqqq' },
    })

    expect(await screen.findByText(/No results for “zzzqqq”/)).toBeInTheDocument()
    expect(screen.queryByText('No matches.')).toBeNull()
  })

  it('reports a failed knowledge search as a failure, not as an empty knowledge base', async () => {
    mockPalette({ id: 'project-1', name: 'Demo', slug: 'demo' }, async () =>
      new Response(JSON.stringify({ detail: 'search backend down' }), {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    renderHarness('/p/demo/events')

    fireEvent.click(screen.getByTestId('open-palette'))
    fireEvent.change(await screen.findByPlaceholderText(/Search projects/i), {
      target: { value: 'zzzqqq' },
    })

    expect(await screen.findByText(/Search failed/)).toBeInTheDocument()
    // A 500 and an empty index both leave `data?.items ?? []` at length 0. The
    // palette must not turn the first into a claim about the user's data.
    expect(screen.queryByText(/No results for/)).toBeNull()
    expect(screen.queryByText('No matches.')).toBeNull()
  })
})

describe('CommandPalette load failure', () => {
  it('keeps the page and offers a reload when the dialog cannot load', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const toastError = vi.spyOn(toast, 'error')
    paletteDialog.fail = true
    renderHarness('/p/demo/events')

    fireEvent.click(screen.getByTestId('open-palette'))

    await waitFor(() => expect(toastError).toHaveBeenCalled())
    expect(at(toastError.mock.calls, 0)[1]).toMatchObject({
      action: expect.objectContaining({ label: 'Reload' }),
    })
    // The failure stays inside the palette: the page around it is untouched.
    expect(screen.getByTestId('open-palette')).toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent('/p/demo/events')
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('CommandPalette focus restore', () => {
  function mockEmptyProject() {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
  }

  it('hands focus back to the button that opened it', async () => {
    mockEmptyProject()
    renderHarness('/p/demo/events')

    const opener = screen.getByTestId('open-palette')
    opener.focus()
    fireEvent.click(opener)
    await screen.findByPlaceholderText(/Search projects/i)

    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })

    await waitFor(() => {
      expect(document.activeElement).toBe(opener)
    })
  })

  it('falls back to the top-bar trigger when Ctrl+K opened it from nowhere', async () => {
    mockEmptyProject()
    renderHarness('/p/demo/events')

    // Nothing focused: exactly the state a global Ctrl+K on a fresh page leaves,
    // where Radix would restore focus to <body>.
    ;(document.activeElement as HTMLElement | null)?.blur()
    expect(document.activeElement).toBe(document.body)

    fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
    await screen.findByPlaceholderText(/Search projects/i)

    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })

    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByTestId('topbar-trigger'))
    })
    expect(document.activeElement).not.toBe(document.body)
  })

  it('skips a trigger that cannot take focus (hidden at this breakpoint)', async () => {
    mockEmptyProject()
    renderHarness('/p/demo/events')

    // The top bar's trigger is display:none at lg+, where focus() on it is a
    // silent no-op. Stand one in front of the harness trigger to model that.
    const hidden = document.createElement('button')
    hidden.setAttribute(COMMAND_PALETTE_TRIGGER_ATTR, '')
    hidden.focus = () => {}
    document.body.prepend(hidden)

    try {
      ;(document.activeElement as HTMLElement | null)?.blur()
      fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
      await screen.findByPlaceholderText(/Search projects/i)

      fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })

      await waitFor(() => {
        expect(document.activeElement).toBe(screen.getByTestId('topbar-trigger'))
      })
    } finally {
      hidden.remove()
    }
  })
})

describe('CommandPalette keyword-first results', () => {
  const PROJECT = {
    id: 'project-1',
    name: 'Demo',
    slug: 'demo',
    description: '',
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
  const result = (title: string, semantic: boolean) => ({
    id: `doc-${title}`,
    entity_type: 'event',
    entity_id: `event-${title}`,
    parent_event_id: `event-${title}`,
    title,
    subtitle: 'Checkout',
    snippet: 'matched text',
    route_path: `/p/demo/events/detail/event-${title}`,
    score: 8,
    highlights: [],
    semantic_used: semantic,
  })

  it('shows the lexical answer while the semantic one is still in flight, then upgrades', async () => {
    let releaseSemantic: (() => void) | null = null
    const asked: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects')) return mockJsonResponse([PROJECT])
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/search?')) {
        asked.push(url)
        if (url.includes('semantic=false')) {
          return mockJsonResponse({
            items: [result('Checkout Completed', false)],
            total: 1,
            semantic_used: false,
          })
        }
        await new Promise<void>(resolve => {
          releaseSemantic = resolve
        })
        return mockJsonResponse({
          items: [result('Checkout Completed', false), result('Order Paid', true)],
          total: 2,
          semantic_used: true,
        })
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderHarness('/p/demo/events')
    fireEvent.click(screen.getByTestId('open-palette'))
    fireEvent.change(await screen.findByPlaceholderText(/Search projects/i), {
      target: { value: 'checkout' },
    })

    // The keyword rows are on screen with the vector leg still unanswered.
    expect(await screen.findByText('Checkout Completed')).toBeInTheDocument()
    expect(screen.queryByText('Order Paid')).not.toBeInTheDocument()
    expect(asked.some(url => url.includes('semantic=false'))).toBe(true)
    expect(asked.some(url => !url.includes('semantic=false'))).toBe(true)

    await waitFor(() => expect(releaseSemantic).not.toBeNull())
    releaseSemantic!()
    expect(await screen.findByText('Order Paid')).toBeInTheDocument()
    expect(screen.getByText('Checkout Completed')).toBeInTheDocument()
  })
})

describe('CommandPalette AI mode and scope', () => {
  function mockAiFetch() {
    return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects')) return mockJsonResponse([demoProject()])
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/ai/status')) return mockJsonResponse({ enabled: true })
      if (url.includes('/api/v1/projects/demo/search?')) {
        return mockJsonResponse({ items: [], total: 0, semantic_used: false })
      }
      if (url.endsWith('/api/v1/projects/demo/ai/ask')) {
        return mockJsonResponse({
          answer: 'Checkout fires on purchase.',
          sources: [{ title: 'Checkout', entity_type: 'event', route_path: '/p/demo/events' }],
          semantic_used: false,
        })
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })
  }

  async function askAi() {
    fireEvent.click(screen.getByTestId('open-palette'))
    fireEvent.change(await screen.findByPlaceholderText(/Search projects/i), {
      target: { value: 'what fires on checkout' },
    })
    fireEvent.click(await screen.findByText(/Ask AI:/))
    await screen.findByText('Checkout fires on purchase.')
  }

  it('opens fresh after an AI source was followed', async () => {
    mockAiFetch()
    renderHarness('/p/demo/events')
    await askAi()

    fireEvent.click(screen.getByRole('button', { name: /Checkout/ }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    fireEvent.click(screen.getByTestId('open-palette'))
    const input = await screen.findByPlaceholderText(/Search projects/i)
    expect(input).toHaveValue('')
    expect(screen.queryByText('Checkout fires on purchase.')).toBeNull()
  })

  it('treats Esc in AI mode as "back to search" and keeps focus in the dialog', async () => {
    mockAiFetch()
    renderHarness('/p/demo/events')
    await askAi()

    const back = screen.getByRole('button', { name: /Back to search/ })
    expect(back).toHaveFocus()

    fireEvent.keyDown(back, { key: 'Escape' })
    expect(await screen.findByPlaceholderText(/Search projects/i)).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('does not search some other project from a workspace route', async () => {
    const fetchSpy = mockAiFetch()
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={authValue}>
          <MemoryRouter initialEntries={['/workspace']}>
            <Routes>
              <Route
                path="/workspace"
                element={
                  <CommandPaletteProvider>
                    <PaletteOpener />
                  </CommandPaletteProvider>
                }
              />
            </Routes>
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )

    fireEvent.click(screen.getByTestId('open-palette'))
    fireEvent.change(await screen.findByPlaceholderText(/Search projects/i), {
      target: { value: 'what fires on checkout' },
    })

    expect(await screen.findByText('Open a project to search its catalog and ask AI.')).toBeInTheDocument()
    // Past the search debounce, so a request would have gone out by now.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 300))
    })
    const urls = fetchSpy.mock.calls.map(([input]) => String(input))
    expect(urls.some((url) => url.includes('/search?') || url.includes('/ai/'))).toBe(false)
    expect(screen.queryByText(/Ask AI:/)).toBeNull()
  })
})

describe('CommandPalette in the shell', () => {
  it('searches the project the shell resolved even when the list does not show it', async () => {
    // A deep link to a project the list has not caught up with: Layout
    // confirmed it through the project endpoint and hands it down.
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/ai/status')) return mockJsonResponse({ enabled: false })
      if (url.includes('/api/v1/projects/demo/search?')) {
        return mockJsonResponse({ items: [], total: 0, semantic_used: false })
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={authValue}>
          <MemoryRouter initialEntries={['/p/demo/events']}>
            <Routes>
              <Route
                path="/p/:slug/events"
                element={
                  <ActiveProjectContext.Provider value={demoProject() as Project}>
                    <CommandPaletteProvider>
                      <PaletteOpener />
                    </CommandPaletteProvider>
                  </ActiveProjectContext.Provider>
                }
              />
            </Routes>
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )

    fireEvent.click(screen.getByTestId('open-palette'))
    fireEvent.change(await screen.findByPlaceholderText(/Search projects/i), {
      target: { value: 'checkout' },
    })

    await waitFor(() => {
      const urls = fetchSpy.mock.calls.map(([input]) => String(input))
      expect(urls.some((url) => url.includes('/api/v1/projects/demo/search?'))).toBe(true)
    })
    expect(screen.queryByText('Open a project to search its catalog and ask AI.')).toBeNull()
  })

  it('leaves focus on the new page’s content after navigating, not on the opener', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects')) return mockJsonResponse([demoProject()])
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    // The shell's shape: one palette above the routes, the page in <main>.
    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={authValue}>
          <MemoryRouter initialEntries={['/p/demo/events']}>
            <Routes>
              <Route
                path="/p/:slug/*"
                element={
                  <CommandPaletteProvider>
                    <PaletteOpener />
                    <main id={MAIN_CONTENT_ID} tabIndex={-1}>
                      <LocationBeacon />
                    </main>
                  </CommandPaletteProvider>
                }
              />
            </Routes>
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )

    const opener = screen.getByTestId('open-palette')
    opener.focus()
    fireEvent.click(opener)
    fireEvent.click(await screen.findByText('Project settings'))

    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/p/demo/settings'))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(document.activeElement).toBe(screen.getByRole('main'))
  })
})

describe('CommandPalette reach and noise (#238)', () => {
  function mockDemo() {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects')) return mockJsonResponse([demoProject()])
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/search?')) {
        return mockJsonResponse({ items: [], total: 0, semantic_used: false })
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })
  }

  it('finds settings sections by what they hold', async () => {
    mockDemo()
    renderHarness('/p/demo/events')
    fireEvent.click(screen.getByTestId('open-palette'))
    const input = await screen.findByPlaceholderText(/Search projects/i)
    await screen.findByText('Demo')

    fireEvent.change(input, { target: { value: 'api key' } })
    expect(await screen.findByText('API keys')).toBeInTheDocument()

    fireEvent.change(input, { target: { value: 'timezone' } })
    expect(await screen.findByText('Project settings: General')).toBeInTheDocument()

    // A keyword only the settings rail knows (nav.ts), not the palette's list.
    fireEvent.change(input, { target: { value: 'dark mode' } })
    expect(await screen.findByText('Profile')).toBeInTheDocument()
  })

  it('matches a multi-word query word by word, and keeps the old page name as a keyword', async () => {
    mockDemo()
    renderHarness('/p/demo/events')
    fireEvent.click(screen.getByTestId('open-palette'))
    const input = await screen.findByPlaceholderText(/Search projects/i)
    await screen.findByText('Demo')

    fireEvent.change(input, { target: { value: 'schema fields' } })
    expect(await screen.findByText('Meta fields')).toBeInTheDocument()
  })

  it('offers create and theme commands', async () => {
    mockDemo()
    renderHarness('/p/demo/events')
    fireEvent.click(screen.getByTestId('open-palette'))
    await screen.findByText('Demo')

    expect(screen.getByText('New event')).toBeInTheDocument()
    expect(screen.getByText('New metric')).toBeInTheDocument()
    expect(screen.getByText(/Switch to (light|dark) theme/)).toBeInTheDocument()
  })

  it('offers branch and invite commands that open their forms', async () => {
    mockDemo()
    renderHarness('/p/demo/events')
    fireEvent.click(screen.getByTestId('open-palette'))
    await screen.findByText('Demo')

    // Without the switcher's cached branch list, one row opens the branches page.
    expect(screen.getByText('Switch branch…')).toBeInTheDocument()
    expect(screen.getByText('Invite member')).toBeInTheDocument()

    fireEvent.click(screen.getByText('New branch'))
    await waitFor(() => {
      expect(screen.getByTestId('location')).toHaveTextContent('/p/demo/branches')
    })
    expect(screen.getByTestId('location-search')).toHaveTextContent('?new=1')
  })

  it('prints no raw route next to a destination', async () => {
    mockDemo()
    renderHarness('/p/demo/events')
    fireEvent.click(screen.getByTestId('open-palette'))
    await screen.findByText('Demo')

    expect(screen.queryByText('/p/demo/meta-fields')).toBeNull()
    expect(screen.queryByText('/settings/members')).toBeNull()
  })
})
