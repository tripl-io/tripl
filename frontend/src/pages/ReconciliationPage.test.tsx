import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type {
  CoverageResponse,
  DeadEventsResponse,
  ShadowEventStatus,
  ShadowEventsResponse,
} from '@/api/reconciliation'
import { DEAD_EVENT_DAYS } from '@/lib/coverage'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { BranchContext } from '@/components/branch-context-internal'
import {
  projectEventTypesKey,
  projectKey,
  projectShadowEventsKey,
  reconciliationCoverageKey,
  reconciliationRootKey,
} from '@/lib/queryKeys'
import ReconciliationPage from './ReconciliationPage'
import { at } from '@/test/at'
import { personaAuth } from '@/test/persona'
import { SessionProject } from '@/test/PersonaProject'

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const coverage: CoverageResponse = {
  days: 14,
  summary: { total_count: 132, matched_count: 124, coverage_pct: 94.2 },
  items: [
    { bucket: '2026-06-01', total_count: 100, matched_count: 95 }, // 95% success
    { bucket: '2026-06-02', total_count: 100, matched_count: 80 }, // 80% warning
    { bucket: '2026-06-03', total_count: 100, matched_count: 50 }, // 50% danger
  ],
}

// Every bucket sits at 100% — no per-day variation, so the panel should switch
// from the dense histogram to a thin steady sparkline.
const steadyCoverage: CoverageResponse = {
  days: 14,
  summary: { total_count: 120, matched_count: 120, coverage_pct: 100 },
  items: [
    { bucket: '2026-06-01', total_count: 50, matched_count: 50 },
    { bucket: '2026-06-02', total_count: 80, matched_count: 80 },
    { bucket: '2026-06-03', total_count: 60, matched_count: 60 },
  ],
}

const shadowNew: ShadowEventsResponse = {
  total: 1,
  new_count: 1,
  items: [
    {
      id: 'sh1',
      scan_config_id: 'scan-1',
      scan_config_name: 'iOS Prod',
      event_type_id: null,
      event_type_name: null,
      event_name: 'variant_color_selected',
      observed_count: 8420,
      first_seen_at: '2026-06-17T11:00:00Z',
      last_seen_at: '2026-06-17T11:56:00Z',
      status: 'new',
      accepted_event_id: null,
    },
  ],
}

const emptyShadow: ShadowEventsResponse = { total: 0, new_count: 0, items: [] }

// GET /projects/demo, trimmed to what the page reads: whether a scan has run.
function projectWith(latestScanStatus: string | null) {
  return {
    slug: 'demo',
    summary: {
      latest_scan_job: latestScanStatus ? { id: 'job-1', status: latestScanStatus } : null,
    },
  }
}

const dead: DeadEventsResponse = {
  days: 30,
  total: 2,
  items: [
    {
      event_id: 'd1',
      name: 'legacy_banner_shown',
      event_type_id: 'et-1',
      event_type_name: 'notification',
      last_seen_at: '2026-05-10T00:00:00Z',
      created_at: '2026-01-01T00:00:00Z',
    },
    {
      event_id: 'd2',
      name: 'promo_code_invalid',
      event_type_id: 'et-2',
      event_type_name: 'checkout',
      last_seen_at: null,
      created_at: '2026-01-01T00:00:00Z',
    },
  ],
}

function statusFromUrl(url: string): ShadowEventStatus {
  if (url.includes('status=accepted')) return 'accepted'
  if (url.includes('status=dismissed')) return 'dismissed'
  return 'new'
}

function mockFetch(): void {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = String(input)
    if (url.includes('/reconciliation/coverage')) return jsonResponse(coverage)
    if (url.includes('/reconciliation/dead-events')) return jsonResponse(dead)
    if (url.includes('/reconciliation/shadow-events')) {
      return jsonResponse(statusFromUrl(url) === 'new' ? shadowNew : emptyShadow)
    }
    if (url.includes('/event-types')) return jsonResponse([])
    if (url.endsWith('/projects/demo')) return jsonResponse(projectWith('completed'))
    throw new Error(`Unhandled fetch: ${url}`)
  })
}

function renderPage(
  auth: AuthContextValue | null = null,
  {
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
    branchId = null,
  }: { queryClient?: QueryClient; branchId?: string | null } = {},
) {
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <SessionProject session={auth}>
          <BranchContext.Provider value={{ branchId, setBranchId: () => {}, slug: 'demo' }}>
            <MemoryRouter initialEntries={['/p/demo/reconciliation']}>
              <Routes>
                <Route path="/p/:slug/reconciliation" element={<ReconciliationPage />} />
              </Routes>
            </MemoryRouter>
          </BranchContext.Provider>
        </SessionProject>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

/** Confirms the archive dialog that now sits in front of every archive (DATA-40). */
async function confirmArchive(): Promise<void> {
  const confirmDialog = await screen.findByRole('alertdialog')
  // The page awaits the confirm promise and then updates state, one microtask
  // after the click: an async act() flushes that continuation too.
  await act(async () => {
    fireEvent.click(within(confirmDialog).getByRole('button', { name: 'Archive' }))
  })
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ReconciliationPage', () => {
  it('offers a viewer no accept, dismiss, archive or selection (DATA-7)', async () => {
    mockFetch()
    renderPage(personaAuth('viewer'))

    expect(await screen.findByText('legacy_banner_shown')).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent(/viewer role/)
    expect(screen.queryByRole('button', { name: 'Accept' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Dismiss' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Archive/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  })

  it('renders the coverage hero and govern header', async () => {
    mockFetch()
    renderPage()

    expect(screen.getByText('Govern')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Reconciliation' })).toBeInTheDocument()
    expect(
      screen.getByText(
        'Compare what your plan defines against what your data sources actually send.',
      ),
    ).toBeInTheDocument()
    expect(await screen.findByText('94%')).toBeInTheDocument()
    // The metric drops the word "coverage" so it no longer collides with the
    // dashboard's plan-coverage KPI — it counts matched occurrences.
    expect(screen.getByText('Data match')).toBeInTheDocument()
    expect(screen.getByText('occurrences matched')).toBeInTheDocument()
    // The headline carries an inline clarifier so it can't be misread as the
    // Coverage page's plan-coverage KPI — they measure different things.
    expect(screen.getByTitle(/tracked event occurrences in warehouse data/i)).toBeInTheDocument()
    expect(screen.queryByText('data-match coverage')).not.toBeInTheDocument()
    expect(
      screen.getByText(
        '124 of 132 tracked event occurrences matched a planned event · 14d',
      ),
    ).toBeInTheDocument()
  })

  it('formats large data-match counts with thousand separators', async () => {
    const bigCoverage: CoverageResponse = {
      days: 14,
      summary: { total_count: 89327935, matched_count: 89327935, coverage_pct: 100 },
      items: [{ bucket: '2026-06-01', total_count: 89327935, matched_count: 89327935 }],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(bigCoverage)
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(dead)
      if (url.includes('/reconciliation/shadow-events')) return jsonResponse(emptyShadow)
      if (url.includes('/event-types')) return jsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderPage()

    expect(
      await screen.findByText(
        '89,327,935 of 89,327,935 tracked event occurrences matched a planned event · 14d',
      ),
    ).toBeInTheDocument()
    // The raw, separator-free rendering must not appear.
    expect(
      screen.queryByText(
        '89327935 of 89327935 tracked event occurrences matched a planned event · 14d',
      ),
    ).not.toBeInTheDocument()
  })

  // tripl-jfm3.26: `coverage_pct` arrives rounded to 2 dp, so 672,190,768 of
  // 672,190,769 comes back as exactly 100.0 and the card printed "100%" over a
  // subtitle that showed an unmatched occurrence.
  it('never prints 100% while an occurrence is unmatched', async () => {
    const nearPerfect: CoverageResponse = {
      days: 14,
      summary: { total_count: 672190769, matched_count: 672190768, coverage_pct: 100 },
      items: [{ bucket: '2026-06-01', total_count: 672190769, matched_count: 672190768 }],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(nearPerfect)
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(dead)
      if (url.includes('/reconciliation/shadow-events')) return jsonResponse(emptyShadow)
      if (url.includes('/event-types')) return jsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderPage()

    expect(await screen.findByText('99.9%')).toBeInTheDocument()
    expect(screen.queryByText('100%')).not.toBeInTheDocument()
  })

  it('rounds an imperfect match DOWN rather than up to the 99.9 ceiling', async () => {
    // 99.6 % would round up to a perfect "100%", so it is held below it — but
    // clamping to the ceiling would report 0.3 pp MORE matched than there is.
    // The headline may only ever understate an imperfect match.
    const almost: CoverageResponse = {
      days: 14,
      summary: { total_count: 1000, matched_count: 996, coverage_pct: 99.6 },
      items: [{ bucket: '2026-06-01', total_count: 1000, matched_count: 996 }],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(almost)
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(dead)
      if (url.includes('/reconciliation/shadow-events')) return jsonResponse(emptyShadow)
      if (url.includes('/event-types')) return jsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderPage()

    expect(await screen.findByText('99.6%')).toBeInTheDocument()
    expect(screen.queryByText('99.9%')).toBeNull()
    expect(screen.queryByText('100%')).toBeNull()
  })

  it('still prints 100% for a genuinely complete match', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(steadyCoverage)
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(dead)
      if (url.includes('/reconciliation/shadow-events')) return jsonResponse(emptyShadow)
      if (url.includes('/event-types')) return jsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderPage()

    expect(await screen.findAllByText('100%')).not.toHaveLength(0)
  })

  it('renders shadow inbox rows with accept/dismiss actions', async () => {
    mockFetch()
    renderPage()

    expect(await screen.findByText('variant_color_selected')).toBeInTheDocument()
    expect(screen.getByText(/8,420 seen/)).toBeInTheDocument()
    expect(screen.getByText('no type')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Accept' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Dismiss' })).toBeInTheDocument()
  })

  it('expands a shadow row to the sample properties the collector kept (DA-32)', async () => {
    const withSamples = {
      ...shadowNew,
      items: [{
        ...shadowNew.items[0],
        sample_properties: [
          { 'event.category': 'product', color: 'red' },
          { 'event.category': 'product', color: 'blue' },
        ],
      }],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(coverage)
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(dead)
      if (url.includes('/reconciliation/shadow-events')) {
        return jsonResponse(statusFromUrl(url) === 'new' ? withSamples : emptyShadow)
      }
      if (url.includes('/event-types')) return jsonResponse([])
      if (url.endsWith('/projects/demo')) return jsonResponse(projectWith('completed'))
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderPage()

    const toggle = await screen.findByRole('button', { name: 'Show 2 samples' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('list', { name: 'Sample properties' })).not.toBeInTheDocument()

    fireEvent.click(toggle)

    const list = screen.getByRole('list', { name: 'Sample properties' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(2)
    expect(within(list).getByText('red')).toBeInTheDocument()
    expect(within(list).getByText('blue')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Hide samples' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('offers no samples toggle for a candidate with none', async () => {
    mockFetch()
    renderPage()

    expect(await screen.findByText('variant_color_selected')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /samples/ })).not.toBeInTheDocument()
  })

  it('renders the dead-events explanation and a calm amber (not danger-red) never marker', async () => {
    mockFetch()
    renderPage()

    expect(await screen.findByText('legacy_banner_shown')).toBeInTheDocument()
    expect(screen.getByText('promo_code_invalid')).toBeInTheDocument()
    // The explanation says when archiving is right (DA-34).
    expect(
      screen.getByText(
        'Seasonal or rarely fired events can show up here; archive only what you have retired.',
      ),
    ).toBeInTheDocument()
    expect(screen.queryByText(/often expected/)).not.toBeInTheDocument()
    // The panel names its window and population, so arriving here from
    // Coverage's own gap panel does not read as two contradictory answers to
    // the same question (tripl-jfm3.23) — and the window it names is the one
    // Coverage counted over, not a second, shorter one (tripl-jfm3.79).
    expect(
      screen.getByText(`Implemented events with no data in the last ${DEAD_EVENT_DAYS} days`),
    ).toBeInTheDocument()
    // "never" reads as a calm amber, never as an alarming danger-red wall.
    const neverRows = screen.getAllByText('never')
    expect(neverRows.length).toBeGreaterThan(0)
    expect(neverRows[0]).toHaveStyle({ color: 'var(--warning)' })
    expect(neverRows[0]).not.toHaveStyle({ color: 'var(--danger)' })
  })

  // Coverage's "Instrumentation gaps" panel links here with "Triage in
  // Reconciliation". This page used to ask for a 14-day window while Coverage
  // counted over 30, so the destination list was a SUPERSET of the count that
  // sent the user here — a shorter window is a weaker silence test
  // (tripl-jfm3.79). CoveragePage.test.tsx pins the other half of the pair.
  it('requests dead events over the same window Coverage counts', async () => {
    mockFetch()
    renderPage()

    await screen.findByText('legacy_banner_shown')

    const deadRequest = vi
      .mocked(globalThis.fetch)
      .mock.calls.map(([input]) => String(input))
      .find((url) => url.includes('/reconciliation/dead-events'))

    expect(deadRequest).toBeDefined()
    expect(deadRequest).toContain(`days=${DEAD_EVENT_DAYS}`)
  })

  it('renders 0-encoded empty segments as a placeholder, never a bare "0"', async () => {
    const deadZero: DeadEventsResponse = {
      days: 30,
      total: 1,
      items: [
        {
          event_id: 'z1',
          name: '0:forecast_for_4:0',
          event_type_id: '',
          event_type_name: '',
          last_seen_at: '2026-05-01T00:00:00Z',
          created_at: '2026-01-01T00:00:00Z',
        },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(coverage)
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(deadZero)
      if (url.includes('/reconciliation/shadow-events')) return jsonResponse(emptyShadow)
      if (url.includes('/event-types')) return jsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderPage()

    expect(await screen.findByText('forecast_for_4')).toBeInTheDocument()
    // The "0" segments collapse to muted placeholders — no confusing standalone "0".
    expect(screen.queryByText('0')).not.toBeInTheDocument()
    expect(screen.getAllByTitle('empty segment')).toHaveLength(2)
  })

  it('renders colon-delimited dead-event names with a placeholder for empty segments', async () => {
    const deadColon: DeadEventsResponse = {
      days: 30,
      total: 2,
      items: [
        {
          event_id: 'c1',
          name: ':forecast_for_4',
          event_type_id: '',
          event_type_name: '',
          last_seen_at: '2026-05-01T00:00:00Z',
          created_at: '2026-01-01T00:00:00Z',
        },
        {
          event_id: 'c2',
          name: 'buoy:copy:coordinates(main)',
          event_type_id: '',
          event_type_name: '',
          last_seen_at: '2026-05-02T00:00:00Z',
          created_at: '2026-01-01T00:00:00Z',
        },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(coverage)
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(deadColon)
      if (url.includes('/reconciliation/shadow-events')) return jsonResponse(emptyShadow)
      if (url.includes('/event-types')) return jsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderPage()

    // A leading empty segment splits the name and surfaces its non-empty piece.
    expect(await screen.findByText('forecast_for_4')).toBeInTheDocument()
    // A name with no empty segment renders as-is (matching the Events list).
    expect(screen.getByText('buoy:copy:coordinates(main)')).toBeInTheDocument()
    // The leading empty segment renders an intentional placeholder, not a blank.
    expect(screen.getByTitle('empty segment')).toBeInTheDocument()
  })

  it('gives each dead-event row a full-name tooltip so ellipsized long names stay distinguishable', async () => {
    // Real names share a long common prefix and only differ near the end, so the
    // truncated rows look identical — the title exposes the full name on hover.
    const longNo = 'page_value_question_page_value_page_value_sail_navigation_interface_no_selected'
    const longYes = 'page_value_question_page_value_page_value_sail_navigation_interface_yes_selected'
    const deadLong: DeadEventsResponse = {
      days: 30,
      total: 2,
      items: [
        {
          event_id: 'l1',
          name: longNo,
          event_type_id: 'et-1',
          event_type_name: 'nav',
          last_seen_at: '2026-05-10T00:00:00Z',
          created_at: '2026-01-01T00:00:00Z',
        },
        {
          event_id: 'l2',
          name: longYes,
          event_type_id: 'et-1',
          event_type_name: 'nav',
          last_seen_at: '2026-05-11T00:00:00Z',
          created_at: '2026-01-01T00:00:00Z',
        },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(coverage)
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(deadLong)
      if (url.includes('/reconciliation/shadow-events')) return jsonResponse(emptyShadow)
      if (url.includes('/event-types')) return jsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderPage()

    // Each row carries its own full name as a native tooltip on the link.
    expect(await screen.findByTitle(longNo)).toHaveAttribute('title', longNo)
    expect(screen.getByTitle(longYes)).toHaveAttribute('title', longYes)
  })

  it('shows a reassuring compact empty state when the new shadow inbox is empty', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(coverage)
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(dead)
      if (url.includes('/reconciliation/shadow-events')) return jsonResponse(emptyShadow)
      if (url.includes('/event-types')) return jsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderPage()

    expect(await screen.findByText('No new events')).toBeInTheDocument()
    expect(
      screen.getByText('No unexpected events seen in the last 14 days.'),
    ).toBeInTheDocument()
  })

  it('switches shadow tabs to show the per-tab empty state', async () => {
    mockFetch()
    renderPage()

    await screen.findByText('variant_color_selected')
    fireEvent.click(screen.getByRole('button', { name: 'Accepted' }))

    expect(await screen.findByText('No accepted events.')).toBeInTheDocument()
  })

  it('prompts to choose an event type before accepting an untyped shadow event', async () => {
    mockFetch()
    renderPage()

    await screen.findByText('variant_color_selected')
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))

    expect(await screen.findByText('Choose event type:')).toBeInTheDocument()
    const select = screen.getByRole('combobox')
    expect(within(select).getByText('Select…')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Confirm' })).toBeDisabled()
    })
  })

  it('shows a thin steady sparkline (not the per-day histogram) when data-match is constant', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(steadyCoverage)
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(dead)
      if (url.includes('/reconciliation/shadow-events')) return jsonResponse(emptyShadow)
      if (url.includes('/event-types')) return jsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderPage()

    // The big number still anchors the panel and conveys the level.
    expect(await screen.findByText('100%')).toBeInTheDocument()
    // A steady line replaces the histogram when coverage never varies.
    expect(
      screen.getByRole('img', { name: /steady at 100% across the window/i }),
    ).toBeInTheDocument()
    // No per-bucket histogram bars are rendered in the steady layout.
    expect(screen.queryByTitle(/2026-06-01:/)).not.toBeInTheDocument()
  })

  it('keeps the per-day histogram (no steady sparkline) when coverage varies', async () => {
    mockFetch()
    renderPage()

    // The default fixture varies (95 / 80 / 50), so the histogram bars remain.
    expect(await screen.findByTitle('2026-06-01: 95%')).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: /steady at/i })).not.toBeInTheDocument()
  })

  it('select-all toggles every dead row and reflects the count on the archive action', async () => {
    mockFetch()
    renderPage()

    await screen.findByText('legacy_banner_shown')
    // With nothing selected the bulk action is present but disabled.
    expect(screen.getByRole('button', { name: 'Archive selected' })).toBeDisabled()

    fireEvent.click(screen.getByRole('checkbox', { name: 'Select all dead events' }))

    const archiveBtn = screen.getByRole('button', { name: 'Archive 2 selected' })
    expect(archiveBtn).toBeEnabled()
    expect(screen.getByRole('checkbox', { name: 'Select legacy_banner_shown' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Select promo_code_invalid' })).toBeChecked()
  })

  it('shows a mixed select-all while only some dead rows are picked, and clears from there (EV-26)', async () => {
    mockFetch()
    renderPage()

    fireEvent.click(await screen.findByRole('checkbox', { name: 'Select legacy_banner_shown' }))
    const selectAll = screen.getByRole('checkbox', { name: 'Select all dead events' })
    expect(selectAll).toHaveAttribute('aria-checked', 'mixed')

    // A click from "some" clears the selection instead of selecting every row.
    fireEvent.click(selectAll)
    expect(screen.getByRole('checkbox', { name: 'Select legacy_banner_shown' })).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Select promo_code_invalid' })).not.toBeChecked()
    expect(screen.getByRole('button', { name: 'Archive selected' })).toBeDisabled()
  })

  it('archives selected dead events and refetches the recon list', async () => {
    const archiveCalls: Array<{ url: string; body: { event_ids: string[]; status: string } }> = []
    let deadPayload: DeadEventsResponse = dead
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(coverage)
      // The archive POST must be matched before the generic dead-events GET.
      if (url.includes('/reconciliation/dead-events/archive')) {
        const body = JSON.parse(String(init?.body)) as { event_ids: string[]; status: string }
        archiveCalls.push({ url, body })
        // Simulate the server archiving the ids: they drop out of the next list.
        deadPayload = {
          ...dead,
          total: dead.items.length - body.event_ids.length,
          items: dead.items.filter((d) => !body.event_ids.includes(d.event_id)),
        }
        return jsonResponse({
          event_ids: body.event_ids,
          status: body.status,
          archived_count: body.event_ids.length,
        })
      }
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(deadPayload)
      if (url.includes('/reconciliation/shadow-events')) return jsonResponse(emptyShadow)
      if (url.includes('/event-types')) return jsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderPage()

    fireEvent.click(
      await screen.findByRole('checkbox', { name: 'Select legacy_banner_shown' }),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Archive 1 selected' }))
    await confirmArchive()

    await waitFor(() => expect(archiveCalls).toHaveLength(1))
    expect(at(archiveCalls, 0).url).toContain('/reconciliation/dead-events/archive')
    expect(at(archiveCalls, 0).body).toEqual({ event_ids: ['d1'], status: 'archived' })

    // After invalidation the archived row is gone; the untouched row survives.
    await waitFor(() => {
      expect(screen.queryByText('legacy_banner_shown')).not.toBeInTheDocument()
    })
    expect(screen.getByText('promo_code_invalid')).toBeInTheDocument()
  })

  it('surfaces an error and keeps the selection when archive fails', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(coverage)
      if (url.includes('/reconciliation/dead-events/archive')) {
        return new Response(JSON.stringify({ detail: 'Event not found on branch' }), {
          status: 404,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(dead)
      if (url.includes('/reconciliation/shadow-events')) return jsonResponse(emptyShadow)
      if (url.includes('/event-types')) return jsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderPage()

    fireEvent.click(
      await screen.findByRole('checkbox', { name: 'Select legacy_banner_shown' }),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Archive 1 selected' }))
    await confirmArchive()

    expect(await screen.findByRole('alert')).toHaveTextContent('Event not found on branch')
    // The row and its selection persist so the user can retry.
    expect(screen.getByText('legacy_banner_shown')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Archive 1 selected' })).toBeInTheDocument()
  })

  // DATA-40: select-all plus one click used to archive the whole list with no
  // confirmation and no word afterwards.
  it('asks before archiving, sends nothing on cancel, and reports what it archived', async () => {
    const archiveCalls: string[][] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(coverage)
      if (url.includes('/reconciliation/dead-events/archive')) {
        const body = JSON.parse(String(init?.body)) as { event_ids: string[]; status: string }
        archiveCalls.push(body.event_ids)
        return jsonResponse({
          event_ids: body.event_ids,
          status: body.status,
          archived_count: body.event_ids.length,
        })
      }
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(dead)
      if (url.includes('/reconciliation/shadow-events')) return jsonResponse(emptyShadow)
      if (url.includes('/event-types')) return jsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderPage()

    fireEvent.click(await screen.findByRole('checkbox', { name: 'Select all dead events' }))
    fireEvent.click(screen.getByRole('button', { name: 'Archive 2 selected' }))

    const confirmDialog = await screen.findByRole('alertdialog')
    expect(confirmDialog).toHaveTextContent('Archive 2 planned events?')
    fireEvent.click(within(confirmDialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(archiveCalls).toHaveLength(0)

    fireEvent.click(screen.getByRole('button', { name: 'Archive 2 selected' }))
    await confirmArchive()

    await waitFor(() => expect(archiveCalls).toEqual([['d1', 'd2']]))
    expect(await screen.findByRole('status')).toHaveTextContent('2 events archived.')
  })

  // DATA-41: accept and archive change the plan, so Coverage's project summary,
  // the event types and the data match must refetch too — not only the list
  // the action came from.
  it('refreshes Coverage, event types and the data match after an archive', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(coverage)
      if (url.includes('/reconciliation/dead-events/archive')) {
        const body = JSON.parse(String(init?.body)) as { event_ids: string[]; status: string }
        return jsonResponse({
          event_ids: body.event_ids,
          status: body.status,
          archived_count: body.event_ids.length,
        })
      }
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(dead)
      if (url.includes('/reconciliation/shadow-events')) return jsonResponse(emptyShadow)
      if (url.includes('/event-types')) return jsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    renderPage(null, { queryClient })

    fireEvent.click(await screen.findByRole('checkbox', { name: 'Select legacy_banner_shown' }))
    fireEvent.click(screen.getByRole('button', { name: 'Archive 1 selected' }))
    await confirmArchive()

    await waitFor(() => {
      expect(invalidate).toHaveBeenCalledWith({ queryKey: projectKey('demo') })
    })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: projectEventTypesKey('demo') })
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: reconciliationCoverageKey('demo', 14),
    })
  })

  // DATA-42: dead events are resolved on main, and archive writes to main. On a
  // feature branch the panel says so and does not offer the write.
  it('labels dead events as main-branch and withholds archive on a feature branch', async () => {
    mockFetch()
    renderPage(null, { branchId: 'branch-1' })

    expect(await screen.findByText('legacy_banner_shown')).toBeInTheDocument()
    expect(
      screen.getByText(
        `Implemented events with no data in the last ${DEAD_EVENT_DAYS} days · main branch`,
      ),
    ).toBeInTheDocument()
    expect(screen.getByText(/Switch to main to archive them/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Archive/ })).not.toBeInTheDocument()
    expect(
      screen.queryByRole('checkbox', { name: 'Select legacy_banner_shown' }),
    ).not.toBeInTheDocument()
    // Shadow triage is branch-scoped and stays available.
    expect(screen.getByRole('button', { name: 'Accept' })).toBeInTheDocument()
    expect(
      vi
        .mocked(globalThis.fetch)
        .mock.calls.some(([input]) => String(input).includes('/dead-events/archive')),
    ).toBe(false)
  })

  // DATA-47: a selected id that a refetch dropped must not ride along into the
  // atomic archive request, which would 404 the whole batch.
  it('drops selected dead events that disappear on refetch', async () => {
    let deadPayload: DeadEventsResponse = dead
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(coverage)
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(deadPayload)
      if (url.includes('/reconciliation/shadow-events')) return jsonResponse(emptyShadow)
      if (url.includes('/event-types')) return jsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    renderPage(null, { queryClient })

    fireEvent.click(await screen.findByRole('checkbox', { name: 'Select all dead events' }))
    expect(screen.getByRole('button', { name: 'Archive 2 selected' })).toBeEnabled()

    deadPayload = { ...dead, total: 1, items: dead.items.slice(1) }
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: reconciliationRootKey() })
    })

    await waitFor(() => {
      expect(screen.queryByText('legacy_banner_shown')).not.toBeInTheDocument()
    })
    expect(screen.getByRole('button', { name: 'Archive 1 selected' })).toBeInTheDocument()
  })

  // DATA-43: the histogram had no accessible name, per-day values only in
  // `title`, and a day without data drew as a 2%-high danger-red bar.
  it('summarises the data-match histogram and marks empty days as no data', async () => {
    const withGap: CoverageResponse = {
      ...coverage,
      items: [...coverage.items, { bucket: '2026-06-04', total_count: 0, matched_count: 0 }],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(withGap)
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(dead)
      if (url.includes('/reconciliation/shadow-events')) return jsonResponse(emptyShadow)
      if (url.includes('/event-types')) return jsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderPage()

    const chart = await screen.findByRole('img', { name: /Data match per day/ })
    expect(chart).toHaveAccessibleName(
      // The window comes from `days`, not the bucket count (DA-3).
      'Data match per day over the last 14 days; lowest 50%; highest 95%; latest 50% on 2026-06-03; 1 day without data',
    )
    expect(screen.getByTitle('2026-06-04: no data')).toBeInTheDocument()
    // The per-day values are reachable without hovering.
    const table = screen.getByRole('table', { name: 'Data match per day' })
    expect(within(table).getByRole('row', { name: '2026-06-04 no data' })).toBeInTheDocument()
    expect(within(table).getByRole('row', { name: '2026-06-01 95%' })).toBeInTheDocument()
  })

  // DATA-39: the inbox stopped at 100 rows without saying so, and triage was one
  // request per click.
  it('says how much of the inbox is shown and loads more on request', async () => {
    const shadowUrls: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(coverage)
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(dead)
      if (url.includes('/reconciliation/shadow-events')) {
        shadowUrls.push(url)
        // One row per page, a different one for each offset.
        const offset = Number(new URL(url, 'http://test').searchParams.get('offset') ?? '0')
        const page: ShadowEventsResponse = {
          total: 250,
          new_count: 250,
          items: [{ ...at(shadowNew.items, 0), id: `sh-${offset}`, event_name: `event_${offset}` }],
        }
        return jsonResponse(page)
      }
      if (url.includes('/event-types')) return jsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderPage()

    expect(await screen.findByText('Showing 1 of 250')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'New 250' })).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(screen.getByRole('button', { name: 'Show more' }))

    // The next page by offset, not a longer first page: every row is reachable.
    expect(await screen.findByText('Showing 2 of 250')).toBeInTheDocument()
    expect(shadowUrls.some((url) => url.includes('offset=1'))).toBe(true)
    expect(shadowUrls.every((url) => url.includes('limit=100'))).toBe(true)
  })

  it('dismisses selected shadow events in bulk, in one request', async () => {
    const typed: ShadowEventsResponse = {
      total: 2,
      new_count: 2,
      items: [
        { ...at(shadowNew.items, 0), id: 'sh1', event_type_id: 'et-1', event_type_name: 'screen' },
        { ...at(shadowNew.items, 0), id: 'sh2', event_name: 'promo_banner_closed' },
      ],
    }
    const batches: { action: string; items: { candidate_id: string }[] }[] = []
    const single: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(coverage)
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(dead)
      if (url.includes('/shadow-events/batch')) {
        const body = JSON.parse(String(init?.body)) as (typeof batches)[number]
        batches.push(body)
        // The second row is refused; the first still goes through.
        return jsonResponse({
          succeeded: 1,
          failed: 1,
          results: [
            { candidate_id: 'sh1', ok: true, status: 'dismissed', event_id: null, error: null, error_status: null },
            { candidate_id: 'sh2', ok: false, status: null, event_id: null, error: 'Candidate already accepted', error_status: 409 },
          ],
        })
      }
      if (/shadow-events\/[^/]+\/(accept|dismiss)/.test(url)) {
        single.push(url)
        return jsonResponse({})
      }
      if (url.includes('/reconciliation/shadow-events')) return jsonResponse(typed)
      if (url.includes('/event-types')) return jsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderPage()

    fireEvent.click(
      await screen.findByRole('checkbox', { name: 'Select all new shadow events' }),
    )
    // Only the typed row can be accepted without choosing a type first.
    expect(screen.getByRole('button', { name: 'Accept 1 selected' })).toBeEnabled()
    expect(screen.getByText('Rows without an event type are accepted one at a time.')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss 2 selected' }))

    await waitFor(() => expect(batches).toHaveLength(1))
    expect(batches[0]).toEqual({
      action: 'dismiss',
      items: [{ candidate_id: 'sh1' }, { candidate_id: 'sh2' }],
    })
    expect(await screen.findByText('1 event dismissed. 1 failed; see the rows below.')).toBeInTheDocument()
    // The refusal lands on its own row, in the server's words.
    expect(screen.getByText('Candidate already accepted')).toBeInTheDocument()
    expect(single).toEqual([])
  })

  // Dismiss only flips a candidate's status: it refreshes the inbox, not the
  // project summary, events and the 14-day data match an accept moves.
  it('refreshes only the inbox after a single dismiss', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(coverage)
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(dead)
      const dismiss = /shadow-events\/([^/]+)\/dismiss/.exec(url)
      if (dismiss?.[1]) return jsonResponse({ candidate_id: dismiss[1], status: 'dismissed' })
      if (url.includes('/reconciliation/shadow-events')) return jsonResponse(shadowNew)
      if (url.includes('/event-types')) return jsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    renderPage(null, { queryClient })

    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss' }))

    await waitFor(() => {
      expect(invalidate).toHaveBeenCalledWith({ queryKey: projectShadowEventsKey('demo') })
    })
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: projectKey('demo') })
    expect(invalidate).not.toHaveBeenCalledWith({
      queryKey: reconciliationCoverageKey('demo', 14),
    })
  })

  // A tab switch mid-run cleared the selection and landed the run's notice in
  // the other tab; the row checkboxes unmounted for the run and shifted rows.
  it('locks the tabs and keeps disabled row checkboxes during a bulk run', async () => {
    const typed: ShadowEventsResponse = {
      total: 2,
      new_count: 2,
      items: [
        { ...at(shadowNew.items, 0), id: 'sh1' },
        { ...at(shadowNew.items, 0), id: 'sh2', event_name: 'promo_banner_closed' },
      ],
    }
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(coverage)
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(dead)
      if (url.includes('/shadow-events/batch')) {
        await gate
        return jsonResponse({
          succeeded: 2,
          failed: 0,
          results: ['sh1', 'sh2'].map((id) => ({
            candidate_id: id, ok: true, status: 'dismissed', event_id: null, error: null, error_status: null,
          })),
        })
      }
      if (url.includes('/reconciliation/shadow-events')) return jsonResponse(typed)
      if (url.includes('/event-types')) return jsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderPage()

    fireEvent.click(
      await screen.findByRole('checkbox', { name: 'Select all new shadow events' }),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss 2 selected' }))

    expect(await screen.findByText('Dismissing 0 of 2…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Accepted' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Dismissed' })).toBeDisabled()
    const rowCheckbox = screen.getByRole('checkbox', { name: 'Select variant_color_selected' })
    expect(rowCheckbox).toBeDisabled()

    await act(async () => {
      release()
    })
    expect(await screen.findByText('2 events dismissed.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Accepted' })).toBeEnabled()
  })
})

describe('ReconciliationPage design review (#248)', () => {
  function mockWith(
    cov: CoverageResponse,
    deadResponse: DeadEventsResponse,
    shadow: ShadowEventsResponse,
    latestScanStatus: string | null = null,
    history: ShadowEventsResponse = emptyShadow,
  ) {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/reconciliation/coverage')) return jsonResponse(cov)
      if (url.includes('/reconciliation/dead-events')) return jsonResponse(deadResponse)
      if (url.includes('/reconciliation/shadow-events')) {
        return jsonResponse(statusFromUrl(url) === 'new' ? shadow : history)
      }
      if (url.includes('/event-types')) return jsonResponse([])
      if (url.endsWith('/projects/demo')) return jsonResponse(projectWith(latestScanStatus))
      throw new Error(`Unhandled fetch: ${url}`)
    })
  }

  // DA-29 / JR-4: an empty project read as an all-clear under a big "0%".
  it('shows one empty state with a path to Scans when there is nothing to reconcile', async () => {
    mockWith(
      { days: 14, summary: { total_count: 0, matched_count: 0, coverage_pct: 0 }, items: [] },
      { days: 30, total: 0, items: [] },
      emptyShadow,
    )
    renderPage()

    expect(
      await screen.findByRole('heading', { name: 'Nothing to reconcile yet' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Go to Scans' })).toHaveAttribute(
      'href',
      '/p/demo/scans',
    )
    expect(screen.queryByText('No new events')).not.toBeInTheDocument()
    expect(screen.queryByText('0%')).not.toBeInTheDocument()
  })

  // JR-4: a project whose scans ran but read nothing lately keeps its panels,
  // so the Accepted / Dismissed history stays one click away.
  it('keeps the panels and the status switch once a scan has run, even with nothing read lately', async () => {
    const accepted: ShadowEventsResponse = {
      total: 1,
      new_count: 0,
      items: [{ ...at(shadowNew.items, 0), id: 'sh-acc', status: 'accepted' }],
    }
    mockWith(
      { days: 14, summary: { total_count: 0, matched_count: 0, coverage_pct: 0 }, items: [] },
      { days: 30, total: 0, items: [] },
      emptyShadow,
      'completed',
      accepted,
    )
    renderPage()

    expect(await screen.findByText('No new events')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Nothing to reconcile yet' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Accepted' }))
    expect(await screen.findByText('variant_color_selected')).toBeInTheDocument()
  })

  it('shows "—" rather than 0% when no occurrence was read but other panels have rows', async () => {
    mockWith(
      { days: 14, summary: { total_count: 0, matched_count: 0, coverage_pct: 0 }, items: [] },
      dead,
      emptyShadow,
    )
    renderPage()

    expect(await screen.findByText('legacy_banner_shown')).toBeInTheDocument()
    expect(screen.getByText('—')).toBeInTheDocument()
    expect(screen.queryByText('0%')).not.toBeInTheDocument()
  })

  // DA-3: 335 hourly buckets read as "the last 335 days" in a 14-day panel.
  it('names the window in days and the bucket as an hour for hourly data', async () => {
    const hourly: CoverageResponse = {
      days: 14,
      summary: { total_count: 300, matched_count: 282, coverage_pct: 94 },
      items: [0, 1, 2].map((h) => ({
        bucket: `2026-06-01T0${h}:00:00Z`,
        total_count: 100,
        matched_count: 94,
      })),
    }
    mockWith(hourly, dead, emptyShadow)
    renderPage()

    expect(
      await screen.findByText('Stable at 94% in every hour with data over the last 14 days'),
    ).toBeInTheDocument()
    expect(screen.queryByText(/on each of the last/)).not.toBeInTheDocument()
  })

  // DA-32 / DA-33: labelled type chip, the scan linked, and the New count
  // carries the warning instead of the whole panel header.
  it('labels the type chip, links the scan and keeps the inbox header neutral', async () => {
    mockWith(coverage, dead, {
      ...shadowNew,
      items: [{ ...at(shadowNew.items, 0), event_type_id: 'et-1', event_type_name: 'screen' }],
    })
    renderPage()

    expect(await screen.findByText('type: screen')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'iOS Prod' })).toHaveAttribute(
      'href',
      '/p/demo/scans/scan-1',
    )
    const newTab = screen.getByRole('button', { name: 'New 1' })
    expect(within(newTab).getByText('1')).toHaveAttribute('data-tone', 'warning')
  })
})
