/**
 * The catalog's half of the coached demo scenario.
 *
 * Rendered inside the REAL DemoScenarioProvider rather than a stub: what has to
 * hold is that a collect the user fired binds the scenario to that metric, and
 * the persisted state is the only honest witness to that — the demo's own tick
 * runs collections constantly, so a spy on the API would prove nothing.
 */

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  MetricCollectNowResponse,
  MetricDefinitionDetailResponse,
  MetricDefinitionListItem,
  MetricDefinitionListResponse,
  Project,
} from '@/types'
import { DemoScenarioProvider } from '@/demo/DemoScenarioProvider'
import {
  buildChapterSteps,
  initialScenarioState,
  readScenarioState,
  writeScenarioState,
  type ScenarioState,
} from '@/demo/scenarioModel'
import { liveLoopState } from '@/demo/scenarioTestState'
import { ApiError, api } from '@/api/client'
import { stopAllMetricCollectionWatches } from '@/hooks/useMetricCollectionWatcher'
import { MetricsCatalog } from './MetricsCatalog'

/** The catalog's filters are FilterSelect chips, not native selects. */
async function pickFilter(label: string, option: string) {
  fireEvent.click(screen.getByRole('combobox', { name: new RegExp(`^${label} filter`) }))
  fireEvent.click(await screen.findByRole('option', { name: option }))
}

vi.mock('@/api/metricsCatalog', () => ({
  metricsCatalogApi: {
    list: vi.fn(),
    get: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    collect: vi.fn(),
    bulkUpdate: vi.fn(),
    reorder: vi.fn(),
  },
}))
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}))
// Owned rows name their owner from the workspace roster.
vi.mock('@/api/users', () => ({
  usersApi: {
    list: vi.fn(async () => [
      { id: 'user-1', email: 'ana@example.com', name: 'Ana Lima', role: 'editor', created_at: '2026-01-01T00:00:00Z' },
    ]),
  },
}))

import { metricsCatalogApi } from '@/api/metricsCatalog'
import { toast } from 'sonner'
import { at } from '@/test/at'

const SLUG = 'demo'
const POLL_MS = 10

const STEPS = buildChapterSteps(SLUG, 'live-loop', initialScenarioState())
const COLLECT_INSTRUCTION = at(STEPS, 2).instruction
const SEE_CHART_INSTRUCTION = at(STEPS, 3).instruction

function demoProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'p-1',
    name: 'Demo',
    slug: SLUG,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    is_demo: true,
    generation_status: 'ready',
    ...overrides,
  } as Project
}

function makeItem(overrides: Partial<MetricDefinitionListItem>): MetricDefinitionListItem {
  return {
    id: 'm-1',
    project_id: 'p-1',
    name: 'checkout_conversion',
    display_name: 'Checkout conversion',
    description: '',
    kind: 'sql',
    status: 'active',
    aggregation: null,
    composition: null,
    interval: '1h',
    color: '#6366f1',
    unit: null,
    anomaly_detection_enabled: true,
    reviewed: false,
    owner_id: null,
    order: 0,
    spark: [1, 2, 3],
    latest_value: 42,
    latest_bucket: null,
    latest_signal: null,
    last_collected_at: null,
    last_collection_status: null,
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-20T00:00:00Z',
    ...overrides,
  }
}

const TWO_METRICS: MetricDefinitionListResponse = {
  items: [
    makeItem({ id: 'm-1', name: 'checkout_conversion', display_name: 'Checkout conversion' }),
    makeItem({ id: 'm-2', name: 'signups', display_name: 'Signups' }),
  ],
  total: 2,
  active_total: 2,
}

/** The collect-metric step, reached the way the user reaches it: a scan landed. */
function collectMetricState(): ScenarioState {
  return liveLoopState('live-loop/collect-metric', {
    scan: { scanConfigId: 'sc-1', scanJobId: 'job-1', startedAt: Date.now() },
  })
}

function seeChartState(metricId: string): ScenarioState {
  return liveLoopState('live-loop/see-chart', { metric: { metricId, startedAt: Date.now() } })
}

function renderCatalog(
  project: Project | undefined = demoProject(),
  path = `/p/${SLUG}/metrics`,
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <DemoScenarioProvider project={project} pollIntervalMs={POLL_MS}>
          <MetricsCatalog slug={SLUG} />
        </DemoScenarioProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

async function openRowMenu(displayName: string) {
  const trigger = await screen.findByRole('button', { name: `Actions for ${displayName}` })
  fireEvent.keyDown(trigger, { key: 'Enter' })
}

const callouts = () => document.querySelectorAll('[data-demo-guide]')

// Radix drives the dropdown through pointer-capture APIs jsdom omits.
beforeAll(() => {
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.setPointerCapture = vi.fn()
  Element.prototype.releasePointerCapture = vi.fn()
})

beforeEach(() => {
  // Module-factory mocks keep their call history across tests otherwise.
  vi.clearAllMocks()
  vi.mocked(metricsCatalogApi.create).mockReset()
  vi.mocked(metricsCatalogApi.bulkUpdate).mockReset()
  vi.mocked(metricsCatalogApi.list).mockReset()
  vi.mocked(metricsCatalogApi.get).mockReset()
  vi.mocked(metricsCatalogApi.collect).mockReset()
  vi.mocked(metricsCatalogApi.list).mockResolvedValue(TWO_METRICS)
  vi.mocked(metricsCatalogApi.collect).mockResolvedValue({
    metric_id: 'm-2',
    status: 'queued',
    window_from: null,
    window_to: null,
    task_id: 'task-1',
  } as unknown as MetricCollectNowResponse)
  // The scenario's own watch polls the definition; leave the run in flight so the
  // step under test does not settle out from under the assertion.
  vi.mocked(metricsCatalogApi.get).mockResolvedValue({
    id: 'm-2',
    last_collection_status: 'running',
  } as MetricDefinitionDetailResponse)
})

afterEach(() => {
  // Collect watches outlive the component by design; end them so one
  // test's poll never reports into the next. Unmount first: stopping a watch
  // notifies every mounted row, an update outside act().
  cleanup()
  stopAllMetricCollectionWatches()
  vi.restoreAllMocks()
  window.localStorage.clear()
})

// A structural assertion on purpose: the defect is not "does it render", it is
// "what is a child of what". @dnd-kit/core 6.3.1 renders its <div role="status">
// live region inline under DndContext, and ARIA's table role admits only row,
// rowgroup and caption children — so a DndContext placed inside role="table"
// puts a foreign role in the grid and axe reports aria-required-children.
describe('MetricsCatalog — the ARIA table owns no live region', () => {
  it("keeps dnd-kit's drag announcements, but outside the table", async () => {
    const { container } = renderCatalog()
    await screen.findByText('Checkout conversion')

    // Half one: the live region still exists. Deleting DndContext would also
    // silence axe, and would cost every screen reader its drag feedback.
    const liveRegion = container.querySelector('[role="status"]')
    expect(liveRegion).not.toBeNull()

    // Half two: it is not inside the grid.
    const table = container.querySelector('[role="table"]')
    expect(table).not.toBeNull()
    // Only the live region is asserted, and that is deliberate. DndContext
    // renders TWO hidden nodes — this one and a `DndDescribedBy-*` div — but
    // both come out of a single Fragment that `Accessibility` either portals
    // whole or leaves inline (@dnd-kit/core 6.3.1,
    // core.cjs.development.js:172-179: `return container ?
    // createPortal(markup, container) : markup`). They can never be on opposite
    // sides of the table, so a second assertion on the describedBy node could
    // not fail on its own — and an assertion that cannot fail is what
    // an earlier bug was about.
    expect(table!.querySelector('[role="status"]')).toBeNull()

    // And the table still has the rowgroups it is required to have, so this is
    // not passing because the table lost its contents.
    expect(table!.querySelectorAll(':scope > [role="rowgroup"]').length).toBe(2)
  })
})

describe('MetricsCatalog — the collect the user fired advances the scenario', () => {
  it('binds the scenario to the metric whose row menu was used', async () => {
    writeScenarioState(SLUG, collectMetricState())
    renderCatalog()

    await openRowMenu('Signups')
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Collect now' }))

    await waitFor(() => expect(metricsCatalogApi.collect).toHaveBeenCalledWith(SLUG, 'm-2'))
    // The scenario now follows m-2 — not the first row, not the tick's own runs.
    await waitFor(() => expect(readScenarioState(SLUG).chapters['live-loop']?.artifacts?.metricId).toBe('m-2'))
  })

  it('leaves a non-demo project with no scenario at all', async () => {
    renderCatalog(demoProject({ is_demo: false }))

    await openRowMenu('Signups')
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Collect now' }))

    await waitFor(() => expect(metricsCatalogApi.collect).toHaveBeenCalledWith(SLUG, 'm-2'))
    // The notify is inert, so nothing is ever persisted for a real project.
    expect(window.localStorage.getItem(`tripl-demo-scenario:${SLUG}`)).toBeNull()
    expect(callouts()).toHaveLength(0)
  })
})

describe('MetricsCatalog — the coach marks', () => {
  it('marks exactly one row for the collect step, not every row', async () => {
    writeScenarioState(SLUG, collectMetricState())
    renderCatalog()

    await screen.findByText('Signups')
    // Two rows, one callout: the mark is an example ("pick a metric"), not an
    // instruction repeated per row.
    await waitFor(() => expect(screen.getAllByText(COLLECT_INSTRUCTION)).toHaveLength(1))
    expect(callouts()).toHaveLength(1)
  })

  it('points see-chart at the row of the metric the scenario is tracking', async () => {
    writeScenarioState(SLUG, seeChartState('m-2'))
    renderCatalog()

    await screen.findByText('Signups')
    await waitFor(() => expect(screen.getAllByText(SEE_CHART_INSTRUCTION)).toHaveLength(1))
    // The collect step is behind the user, so its mark is gone.
    expect(screen.queryByText(COLLECT_INSTRUCTION)).not.toBeInTheDocument()
  })

  it('renders no callout for a project that is not a demo', async () => {
    writeScenarioState(SLUG, collectMetricState())
    renderCatalog(demoProject({ is_demo: false }))

    await screen.findByText('Signups')
    expect(callouts()).toHaveLength(0)
    expect(screen.queryByText(COLLECT_INSTRUCTION)).not.toBeInTheDocument()
  })
})

const NOT_A_DEMO = demoProject({ is_demo: false })

function listCallParams(index: number) {
  return at(vi.mocked(metricsCatalogApi.list).mock.calls, index)[1]
}

describe('MetricsCatalog — the whole catalog, not its first page', () => {
  it('walks every page and offers reorder over the full list', async () => {
    const third = makeItem({ id: 'm-3', name: 'refunds', display_name: 'Refunds' })
    vi.mocked(metricsCatalogApi.list).mockImplementation(async (_slug, params) =>
      params?.offset
        ? { items: [third], total: 3, active_total: 3 }
        : { ...TWO_METRICS, total: 3, active_total: 3 },
    )
    renderCatalog(NOT_A_DEMO)

    expect(await screen.findByText('Refunds')).toBeInTheDocument()
    expect(screen.getByText('Checkout conversion')).toBeInTheDocument()
    expect(listCallParams(0)).toMatchObject({ offset: 0, limit: 1000 })
    expect(listCallParams(1)).toMatchObject({ offset: 2, limit: 1000 })
    // Every row arrived, so reordering is allowed — on all three.
    expect(screen.getByRole('button', { name: 'Reorder Refunds' })).toBeInTheDocument()
    expect(screen.queryByText(/Showing 2 of 3 metrics/)).not.toBeInTheDocument()
  })

  it('says when rows are missing and turns reorder off', async () => {
    // The server claims more rows than it will hand over.
    vi.mocked(metricsCatalogApi.list).mockImplementation(async (_slug, params) =>
      params?.offset
        ? { items: [], total: 5, active_total: 5 }
        : { ...TWO_METRICS, total: 5, active_total: 5 },
    )
    renderCatalog(NOT_A_DEMO)

    expect(await screen.findByText(/Showing 2 of 5 metrics/)).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Reorder Checkout conversion' }),
    ).not.toBeInTheDocument()
  })
})

describe('MetricsCatalog — filters keep the rows while they load', () => {
  it('leaves the previous rows up instead of blanking to Loading', async () => {
    renderCatalog(NOT_A_DEMO)
    await screen.findByText('Signups')

    // The next list never answers, so what shows is what the placeholder keeps.
    vi.mocked(metricsCatalogApi.list).mockImplementation(() => new Promise(() => {}))
    await pickFilter('Status', 'Draft')

    await waitFor(() =>
      expect(metricsCatalogApi.list).toHaveBeenCalledWith(
        SLUG,
        expect.objectContaining({ status: ['draft'] }),
      ),
    )
    expect(screen.getByText('Signups')).toBeInTheDocument()
    expect(screen.queryByText('Loading metrics…')).not.toBeInTheDocument()
    expect(screen.getByRole('table', { name: 'Metrics' })).toHaveAttribute('aria-busy', 'true')
  })
})

describe('MetricsCatalog — first load (#237)', () => {
  it('draws skeleton rows and skeleton stat values, never "—" or a toned 0', async () => {
    vi.mocked(metricsCatalogApi.list).mockImplementation(() => new Promise(() => {}))
    renderCatalog(NOT_A_DEMO)

    expect(await screen.findByText('Loading metrics…')).toBeInTheDocument()
    expect(screen.queryByText('—')).not.toBeInTheDocument()
  })
})

describe('MetricsCatalog — filters live in the URL', () => {
  it('restores search, status and the stat filter from the address', async () => {
    renderCatalog(NOT_A_DEMO, `/p/${SLUG}/metrics?q=sign&status=active&signal=anomalies`)

    await waitFor(() =>
      expect(metricsCatalogApi.list).toHaveBeenCalledWith(
        SLUG,
        expect.objectContaining({ search: 'sign', status: ['active'] }),
      ),
    )
    expect(screen.getByLabelText('Search metrics')).toHaveValue('sign')
    expect(screen.getByRole('combobox', { name: /^Status filter/ })).toHaveTextContent(/Status:\s*Active/)
    expect(screen.getByRole('button', { name: /^With anomalies\b/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  it('keeps typed search text local and writes only the settled value to the address', async () => {
    // Bound straight to `q`, the box was reset to the old URL value between a
    // keystroke and the async navigation commit: caret jumps, lost characters.
    function Probe() {
      const location = useLocation()
      const navigate = useNavigate()
      return (
        <>
          <output data-testid="address">{location.search}</output>
          <button type="button" onClick={() => navigate(`/p/${SLUG}/metrics?q=signups`)}>
            Go elsewhere
          </button>
        </>
      )
    }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/p/${SLUG}/metrics`]}>
          <DemoScenarioProvider project={NOT_A_DEMO} pollIntervalMs={POLL_MS}>
            <MetricsCatalog slug={SLUG} />
            <Probe />
          </DemoScenarioProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await screen.findByText('Signups')
    const box = screen.getByLabelText('Search metrics')

    fireEvent.change(box, { target: { value: 'ch' } })
    fireEvent.change(box, { target: { value: 'chec' } })
    expect(box).toHaveValue('chec')
    // Not one history write per keystroke: the address waits for a pause.
    expect(screen.getByTestId('address')).toHaveTextContent(/^$/)
    await waitFor(() => expect(screen.getByTestId('address')).toHaveTextContent('?q=chec'))
    // Its own write landing does not reset what the user sees.
    expect(box).toHaveValue('chec')

    // A change from outside (Back, a link) still wins.
    fireEvent.click(screen.getByRole('button', { name: 'Go elsewhere' }))
    await waitFor(() => expect(box).toHaveValue('signups'))
  })

  it('filters by review status from the address', async () => {
    renderCatalog(NOT_A_DEMO, `/p/${SLUG}/metrics?review=unreviewed`)

    await waitFor(() =>
      expect(metricsCatalogApi.list).toHaveBeenCalledWith(
        SLUG,
        expect.objectContaining({ reviewed: false }),
      ),
    )
    expect(screen.getByRole('combobox', { name: /^Review status filter/ })).toHaveTextContent(
      /Review status:\s*Not reviewed/,
    )
  })

  it('renders the stat filters as MiniStat toggles, not a padded wrapper', async () => {
    // A padded role=button div around the stat shifted the phone grid's
    // second row sideways; the primitive's own button cancels its padding.
    renderCatalog(NOT_A_DEMO, `/p/${SLUG}/metrics?signal=stale`)

    const stale = await screen.findByRole('button', { name: /^Stale\b/ })
    expect(stale.tagName).toBe('BUTTON')
    expect(stale).toHaveAttribute('data-slot', 'mini-stat-pressable')
    expect(stale).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: /^With anomalies\b/ })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
  })

  it('ignores values that are not filters', async () => {
    renderCatalog(NOT_A_DEMO, `/p/${SLUG}/metrics?status=bogus&signal=bogus&review=bogus`)

    await screen.findByText('Signups')
    expect(listCallParams(0)).toMatchObject({ status: undefined, reviewed: undefined })
    expect(screen.getByRole('button', { name: /^With anomalies\b/ })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
  })
})

describe('MetricsCatalog — an empty filter result has a way out', () => {
  it('names the active filters and clears them in one click', async () => {
    vi.mocked(metricsCatalogApi.list).mockImplementation(async (_slug, params) =>
      params?.search ? { items: [], total: 0, active_total: 0 } : TWO_METRICS,
    )
    renderCatalog(NOT_A_DEMO, `/p/${SLUG}/metrics?q=nothing`)

    expect(await screen.findByText('No metrics match search “nothing”.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }))

    expect(await screen.findByText('Signups')).toBeInTheDocument()
    expect(screen.getByLabelText('Search metrics')).toHaveValue('')
  })
})

describe('MetricsCatalog — one Tab stop per row destination', () => {
  it('keeps rows out of the tab order and the name link in it', async () => {
    renderCatalog(NOT_A_DEMO)
    const link = await screen.findByRole('link', { name: 'Signups' })

    const row = link.closest('[role="row"]') as HTMLElement
    expect(row).not.toHaveAttribute('tabindex')
    expect(within(row).getByRole('link', { name: 'Signups' })).toBe(link)
  })
})

describe('MetricsCatalog — collect completion outlives the row', () => {
  it('still reports the finished run after the catalog unmounts', async () => {
    let finishRun: (definition: MetricDefinitionDetailResponse) => void = () => {}
    vi.mocked(metricsCatalogApi.get).mockImplementation(
      () =>
        new Promise(resolve => {
          finishRun = resolve
        }),
    )
    const view = renderCatalog(NOT_A_DEMO)

    await openRowMenu('Signups')
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Collect now' }))
    await waitFor(() => expect(metricsCatalogApi.get).toHaveBeenCalledWith(SLUG, 'm-2'))

    // Leaving the page used to end the watch without a word.
    view.unmount()
    finishRun({ id: 'm-2', last_collection_status: 'success' } as MetricDefinitionDetailResponse)

    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith('"Signups" collected — the chart is up to date.'),
    )
  })
})

describe('MetricsCatalog — duplicate as draft', () => {
  it('appends an unreviewed copy and steps past a name the filtered list hid', async () => {
    vi.mocked(metricsCatalogApi.get).mockResolvedValue({
      id: 'm-2',
      name: 'signups',
      display_name: 'Signups',
      kind: 'sql',
      order: 7,
      reviewed: true,
      interval: '1h',
      data_source_id: 'ds-1',
      config: { metric_sql: 'SELECT 1', time_column: 'ts' },
    } as unknown as MetricDefinitionDetailResponse)
    vi.mocked(metricsCatalogApi.create)
      .mockRejectedValueOnce(new ApiError('Metric definition with this name already exists', 409))
      .mockResolvedValueOnce({ id: 'm-copy' } as Awaited<ReturnType<typeof metricsCatalogApi.create>>)
    renderCatalog(NOT_A_DEMO)

    await openRowMenu('Signups')
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Duplicate as draft' }))

    await waitFor(() => expect(metricsCatalogApi.create).toHaveBeenCalledTimes(2))
    const [, first] = at(vi.mocked(metricsCatalogApi.create).mock.calls, 0)
    const [, second] = at(vi.mocked(metricsCatalogApi.create).mock.calls, 1)
    expect(first).toMatchObject({ name: 'signups_copy', order: 0, reviewed: false, status: 'draft' })
    expect(second).toMatchObject({ name: 'signups_copy_2' })
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Metric duplicated as a draft.'))
  })
})

describe('MetricsCatalog — status changes can be undone', () => {
  it('offers Undo on a bulk archive and restores what each metric was', async () => {
    vi.mocked(metricsCatalogApi.list).mockResolvedValue({
      items: [
        makeItem({ id: 'm-1', name: 'checkout_conversion', display_name: 'Checkout conversion' }),
        makeItem({ id: 'm-2', name: 'signups', display_name: 'Signups', status: 'draft' }),
      ],
      total: 2,
      active_total: 1,
    })
    vi.mocked(metricsCatalogApi.bulkUpdate).mockResolvedValue(undefined)
    renderCatalog(NOT_A_DEMO)

    fireEvent.click(await screen.findByRole('checkbox', { name: 'Select all metrics' }))
    fireEvent.click(screen.getByRole('button', { name: 'Set archived' }))

    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        '2 metrics set to archived.',
        expect.objectContaining({ action: expect.objectContaining({ label: 'Undo' }) }),
      ),
    )
    const call = vi
      .mocked(toast.success)
      .mock.calls.find(([message]) => message === '2 metrics set to archived.')
    const options = call?.[1] as unknown as { action: { onClick: () => void } }
    options.action.onClick()

    await waitFor(() => {
      expect(metricsCatalogApi.bulkUpdate).toHaveBeenCalledWith(SLUG, {
        metric_ids: ['m-1'],
        status: 'active',
      })
      expect(metricsCatalogApi.bulkUpdate).toHaveBeenCalledWith(SLUG, {
        metric_ids: ['m-2'],
        status: 'draft',
      })
    })
  })
})

// values and dates paint in the app locale, so the stat strip beside
// them must too — a bare toLocaleString() printed "1.234" next to "1,234" for a
// de-DE reader.
describe('MetricsCatalog — the stat strip uses the app locale', () => {
  it('formats its counts through the app locale, never the browser one', async () => {
    const original = Number.prototype.toLocaleString
    vi.spyOn(Number.prototype, 'toLocaleString').mockImplementation(function (
      this: number,
      ...args: Parameters<number['toLocaleString']>
    ) {
      return args[0] === undefined ? 'BROWSER-LOCALE' : original.apply(this, args)
    })
    renderCatalog(NOT_A_DEMO)

    await screen.findByRole('link', { name: 'Signups' })
    const metricsStat = screen.getByText('Metrics', { selector: 'dt' }).closest('dl') as HTMLElement
    expect(within(metricsStat).getByRole('definition')).toHaveTextContent('2')
    expect(screen.queryByText(/BROWSER-LOCALE/)).not.toBeInTheDocument()
  })
})

describe('MetricsCatalog — the stat strip summarises the whole catalog', () => {
  it('keeps the project counts while a search narrows the table', async () => {
    vi.mocked(metricsCatalogApi.list).mockImplementation(async (_slug, params) =>
      params?.search
        ? { items: [], total: 0, active_total: 0 }
        : TWO_METRICS,
    )
    renderCatalog(NOT_A_DEMO, `/p/${SLUG}/metrics?q=nothing`)

    expect(await screen.findByText(/No metrics match/)).toBeInTheDocument()
    const metricsStat = screen.getByText('Metrics', { selector: 'dt' }).closest('dl') as HTMLElement
    await waitFor(() => expect(within(metricsStat).getByRole('definition')).toHaveTextContent('2'))
    expect(screen.getByText('0 of 2 metrics')).toBeInTheDocument()
  })
})

describe('MetricsCatalog — review state and archive toast', () => {
  it('marks a reviewed metric and lets the row menu toggle it', async () => {
    vi.mocked(metricsCatalogApi.list).mockResolvedValue({
      ...TWO_METRICS,
      items: [
        makeItem({ id: 'm-1', display_name: 'Checkout conversion', reviewed: true }),
        makeItem({ id: 'm-2', name: 'signups', display_name: 'Signups' }),
      ],
    })
    vi.mocked(metricsCatalogApi.update).mockResolvedValue(
      {} as Awaited<ReturnType<typeof metricsCatalogApi.update>>,
    )
    renderCatalog(NOT_A_DEMO)

    expect(await screen.findByTitle('Reviewed')).toBeInTheDocument()
    await openRowMenu('Signups')
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Mark reviewed' }))
    await waitFor(() =>
      expect(metricsCatalogApi.update).toHaveBeenCalledWith(SLUG, 'm-2', { reviewed: true }),
    )
  })

  it('shows the owner of an owned metric and nothing on an unowned one', async () => {
    vi.mocked(metricsCatalogApi.list).mockResolvedValue({
      ...TWO_METRICS,
      items: [
        makeItem({ id: 'm-1', display_name: 'Checkout conversion', owner_id: 'user-1' }),
        makeItem({ id: 'm-2', name: 'signups', display_name: 'Signups' }),
      ],
    })
    renderCatalog(NOT_A_DEMO)

    expect(await screen.findByRole('img', { name: 'Owner: Ana Lima' })).toBeInTheDocument()
    expect(screen.getAllByRole('img', { name: /^Owner:/ })).toHaveLength(1)
  })

  it('names the archived metric and keeps the Undo toast up for 10 seconds', async () => {
    vi.mocked(metricsCatalogApi.update).mockResolvedValue(
      {} as Awaited<ReturnType<typeof metricsCatalogApi.update>>,
    )
    renderCatalog(NOT_A_DEMO)

    await openRowMenu('Signups')
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Archive' }))
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        '“Signups” archived — collection stopped.',
        expect.objectContaining({ duration: 10_000 }),
      ),
    )
  })
})

describe('MetricsCatalog — narrowed to one fact table (F7)', () => {
  it('lists the metrics that read the table and clears back to the whole catalog', async () => {
    // The table names come through the shared client.
    vi.spyOn(api, 'get').mockImplementation((async (path: string) => {
      if (path.endsWith('/fact-tables')) {
        return { items: [{ id: 'ft-1', display_name: 'Orders' }], total: 1 }
      }
      return { items: [], total: 0, active_total: 0 }
    }) as never)
    vi.mocked(metricsCatalogApi.list).mockImplementation(async (_slug, params) =>
      params?.fact_table_id === 'ft-1'
        ? {
            items: [makeItem({ id: 'm-1', name: 'order_count', display_name: 'Order count' })],
            total: 1,
            active_total: 1,
          } as never
        : TWO_METRICS,
    )
    renderCatalog(NOT_A_DEMO, `/p/${SLUG}/metrics?fact_table=ft-1`)

    expect(await screen.findByText('Order count')).toBeInTheDocument()
    expect(metricsCatalogApi.list).toHaveBeenCalledWith(
      SLUG,
      expect.objectContaining({ fact_table_id: 'ft-1' }),
    )
    const chip = await screen.findByRole('button', {
      name: 'Stop showing only metrics that read Orders',
    })

    fireEvent.click(chip)
    // Back on the shared client's unfiltered list.
    expect(await screen.findByText('Signups')).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /Stop showing only metrics that read/ }),
    ).toBeNull()
  })
})
