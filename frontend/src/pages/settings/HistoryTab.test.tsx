/**
 * Plan history — the revision list is the only place a snapshot's branch is
 * named, so what it renders about a revision has to survive a narrow card.
 * Both assertions here are about text and attributes, not layout:
 * the CSS truncation itself is not observable in jsdom, but the tooltip that
 * makes it recoverable and the separator-joined metadata are.
 */

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { PlanDiff, PlanRevisionSummary } from '@/types'
import { HistoryTab } from './HistoryTab'

vi.mock('@/api/planRevisions', () => ({
  planRevisionsApi: { list: vi.fn(), create: vi.fn(), get: vi.fn(), diff: vi.fn() },
}))

// The page names revision authors and links branch revisions to their review.
vi.mock('@/api/planBranches', () => ({
  planBranchesApi: { list: vi.fn(async () => ({ items: [], total: 0 })) },
}))
vi.mock('@/api/users', () => ({
  usersApi: { list: vi.fn(async () => []) },
}))

// The schema export (GH #262): the API is mocked and the download itself is
// replaced, so the test sees the bundle and the file name it would save.
vi.mock('@/api/planExport', () => ({
  planExportApi: { jsonSchema: vi.fn() },
}))
vi.mock('./planSchemaDownload', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./planSchemaDownload')>()),
  downloadJson: vi.fn(),
}))

import { planBranchesApi } from '@/api/planBranches'
import { planExportApi } from '@/api/planExport'
import { planRevisionsApi } from '@/api/planRevisions'
import { BranchContext } from '@/components/branch-context-internal'
import { downloadJson } from './planSchemaDownload'

const SLUG = 'demo'

// The summary a real branch creation writes:
// plan_branch_service.py:710 → f"Base snapshot for branch '{name}'".
const BRANCH_SUMMARY = "Base snapshot for branch 'feature/checkout-funnel'"

function makeRevision(overrides: Partial<PlanRevisionSummary> = {}): PlanRevisionSummary {
  return {
    id: 'rev-1',
    project_id: 'p-1',
    summary: BRANCH_SUMMARY,
    created_at: '2026-08-19T10:27:00Z',
    created_by: null,
    kind: 'branch_base',
    branch_id: 'b-7',
    entity_counts: {
      event_types: 3,
      fields: 10,
      events: 18,
      variables: 0,
      meta_fields: 0,
      relations: 0,
    },
    ...overrides,
  }
}

function renderHistory() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <HistoryTab slug={SLUG} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.mocked(planRevisionsApi.list).mockReset()
  vi.mocked(planRevisionsApi.list).mockResolvedValue({ items: [makeRevision()], total: 1 })
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('HistoryTab — a revision row keeps its identity readable', () => {
  it('titles the summary with the full string, so a clipped branch name survives', async () => {
    renderHistory()

    const summary = await screen.findByText(BRANCH_SUMMARY)
    // Without this the name clipped to "Base snapshot for branch 'feature/c…"
    // and there was nothing else on screen naming the branch.
    expect(summary).toHaveAttribute('title', BRANCH_SUMMARY)
  })

  it('renders the metadata as one separator-joined line that cannot end on a "·"', async () => {
    renderHistory()

    await screen.findByText(BRANCH_SUMMARY)
    const meta = screen.getByText(/types · .* fields · .* events$/)
    expect(meta.textContent).toMatch(/^.+ · 3 types · 10 fields · 18 events$/)
    // The old markup interleaved bare "·" text nodes between the counts, so a
    // wrap could leave one dangling as the last glyph of a line. One string
    // with the separators inside it can only ever be truncated with an ellipsis.
    expect(meta).toHaveAttribute('title', meta.textContent)
  })
})

describe('HistoryTab — what wrote each revision', () => {
  it('folds branch openings away when the page has merges or snapshots, and shows them on request', async () => {
    vi.mocked(planRevisionsApi.list).mockResolvedValue({
      items: [
        makeRevision({
          id: 'rev-3',
          summary: "Merged branch 'feature/checkout-funnel'",
          kind: 'merge',
        }),
        makeRevision({ id: 'rev-2', summary: BRANCH_SUMMARY }),
        makeRevision({
          id: 'rev-1',
          summary: 'Before the v2 launch',
          kind: 'snapshot',
          branch_id: null,
        }),
      ],
      total: 3,
    })
    vi.mocked(planBranchesApi.list).mockResolvedValue({
      items: [
        {
          id: 'b-7',
          project_id: 'p-1',
          name: 'feature/checkout-funnel',
          kind: 'working',
          status: 'merged',
          description: '',
          base_revision_id: null,
          created_by: null,
          merged_at: null,
          merged_by: null,
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
        },
      ],
      total: 1,
    })
    vi.mocked(planRevisionsApi.diff).mockResolvedValue({
      revision_id: 'rev-3',
      compare_to: 'rev-2',
      entries: [],
      summary: { added: 0, removed: 0, changed: 0 },
    })
    renderHistory()

    expect(await screen.findByText("Merged branch 'feature/checkout-funnel'")).toBeInTheDocument()
    expect(screen.getByText('Before the v2 launch')).toBeInTheDocument()
    expect(screen.queryByText(BRANCH_SUMMARY)).not.toBeInTheDocument()

    // The selected merge links to the branch it came from.
    expect(await screen.findByRole('link', { name: 'feature/checkout-funnel' })).toHaveAttribute(
      'href',
      '/p/demo/branches/b-7',
    )

    fireEvent.click(screen.getByRole('button', { name: 'Show branch openings' }))
    expect(screen.getByText(BRANCH_SUMMARY)).toBeInTheDocument()
  })

  it('reads the kind from the revision, not from a summary that only looks like a merge', async () => {
    vi.mocked(planRevisionsApi.list).mockResolvedValue({
      items: [
        makeRevision({
          id: 'rev-1',
          summary: "Merged branch 'feature/checkout-funnel'",
          kind: 'snapshot',
          branch_id: null,
        }),
      ],
      total: 1,
    })
    renderHistory()

    // A user's snapshot titled like a merge stays a snapshot, with no branch link.
    expect(await screen.findAllByRole('img', { name: 'Snapshot' })).not.toHaveLength(0)
    expect(screen.queryByRole('img', { name: 'Merge' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('links a merge by its branch_id and labels it with the branch\'s current name', async () => {
    vi.mocked(planRevisionsApi.list).mockResolvedValue({
      items: [
        makeRevision({ id: 'rev-2', summary: "Merged branch 'old-name'", kind: 'merge' }),
      ],
      total: 1,
    })
    vi.mocked(planBranchesApi.list).mockResolvedValue({
      items: [
        {
          id: 'b-7',
          project_id: 'p-1',
          name: 'renamed/branch',
          kind: 'working',
          status: 'merged',
          description: '',
          base_revision_id: null,
          created_by: null,
          merged_at: null,
          merged_by: null,
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
        },
      ],
      total: 1,
    })
    renderHistory()

    expect(await screen.findByRole('link', { name: 'renamed/branch' })).toHaveAttribute(
      'href',
      '/p/demo/branches/b-7',
    )
  })

  it('offers "Snapshot now" as a secondary action that says what a snapshot is for', async () => {
    renderHistory()

    fireEvent.click(await screen.findByRole('button', { name: 'Snapshot now' }))
    expect(await screen.findByText(/named checkpoint of the plan/)).toBeInTheDocument()
  })
})

describe('HistoryTab — a large diff stays readable', () => {
  it('groups entries by type, starts them closed, and filters by kind', async () => {
    vi.mocked(planRevisionsApi.list).mockResolvedValue({
      items: [makeRevision({ id: 'rev-2' }), makeRevision({ id: 'rev-1' })],
      total: 2,
    })
    const added = (name: string) => ({
      entity_type: 'field_definition' as const,
      kind: 'added' as const,
      name,
      parent: 'checkout',
      changes: [`Added field ${name}`],
    })
    vi.mocked(planRevisionsApi.diff).mockResolvedValue({
      revision_id: 'rev-2',
      compare_to: 'rev-1',
      summary: { added: 6, removed: 0, changed: 1 },
      entries: [
        {
          entity_type: 'event',
          kind: 'changed',
          name: 'purchase',
          parent: null,
          changes: ['status'],
          field_changes: [{ field: 'status', before: 'draft', after: 'live' }],
        },
        ...['a', 'b', 'c', 'd', 'e', 'f'].map(added),
      ],
    })
    renderHistory()

    const fields = await screen.findByRole('region', { name: 'Fields' })
    expect(within(fields).getByRole('heading', { name: 'Fields · 6' })).toBeInTheDocument()
    // Seven entries: collapsed until opened.
    const purchase = screen.getByRole('button', { name: /purchase/ })
    expect(purchase).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(purchase)
    expect(screen.getByText('live')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '+6 added' }))
    expect(screen.queryByRole('region', { name: 'Fields' })).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Events' })).toBeInTheDocument()
  })
})

describe('HistoryTab — diff failure', () => {
  it('reports a failed diff through the shared error card', async () => {
    vi.mocked(planRevisionsApi.list).mockResolvedValue({
      items: [makeRevision({ id: 'rev-2' }), makeRevision({ id: 'rev-1' })],
      total: 2,
    })
    vi.mocked(planRevisionsApi.diff).mockRejectedValue(new Error('boom'))
    renderHistory()

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByRole('heading', { level: 3, name: 'Failed to load diff' })).toBeInTheDocument()
    expect(alert).toHaveTextContent('boom')
  })
})

describe('HistoryTab — paging past the first page', () => {
  function page(ids: string[]): PlanRevisionSummary[] {
    return ids.map((id) => makeRevision({ id, summary: `Snapshot ${id}` }))
  }

  it('asks for one row past the page and diffs the last row against it', async () => {
    const ids = Array.from({ length: 51 }, (_, index) => `rev-${index}`)
    vi.mocked(planRevisionsApi.list).mockResolvedValue({ items: page(ids), total: 120 })
    vi.mocked(planRevisionsApi.diff).mockResolvedValue({
      revision_id: 'rev-49',
      compare_to: 'rev-50',
      entries: [],
      summary: { added: 0, removed: 0, changed: 0 },
    })
    renderHistory()

    expect(await screen.findByText('Snapshot rev-49')).toBeInTheDocument()
    expect(planRevisionsApi.list).toHaveBeenCalledWith(SLUG, { offset: 0, limit: 51 })
    // The extra row is the diff base, not a 51st row on the page.
    expect(screen.queryByText('Snapshot rev-50')).not.toBeInTheDocument()

    fireEvent.click(screen.getByText('Snapshot rev-49'))
    await waitFor(() =>
      expect(planRevisionsApi.diff).toHaveBeenCalledWith(SLUG, 'rev-49', 'rev-50'),
    )
    expect(screen.queryByText(/This is the oldest revision/)).not.toBeInTheDocument()
  })

  it('pages to older revisions', async () => {
    const ids = Array.from({ length: 51 }, (_, index) => `rev-${index}`)
    vi.mocked(planRevisionsApi.list).mockResolvedValue({ items: page(ids), total: 120 })
    vi.mocked(planRevisionsApi.diff).mockResolvedValue({
      revision_id: 'x',
      compare_to: 'y',
      entries: [],
      summary: { added: 0, removed: 0, changed: 0 },
    })
    renderHistory()

    expect(await screen.findByText('Showing 1–50 of 120 revisions.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Older' }))

    await waitFor(() =>
      expect(planRevisionsApi.list).toHaveBeenCalledWith(SLUG, { offset: 50, limit: 51 }),
    )
  })

  it('shows a failed load as an error with a retry, not as "No revisions yet"', async () => {
    vi.mocked(planRevisionsApi.list).mockRejectedValue(new Error('boom'))
    renderHistory()

    expect(await screen.findByText("Couldn't load plan history")).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(screen.queryByText(/No revisions yet/)).not.toBeInTheDocument()
  })
})

describe('HistoryTab — a diff says what changed, not only where', () => {
  it('renders before and after values, in the branch review words', async () => {
    vi.mocked(planRevisionsApi.list).mockResolvedValue({
      items: [makeRevision({ id: 'rev-2' }), makeRevision({ id: 'rev-1' })],
      total: 2,
    })
    const diff: PlanDiff = {
      revision_id: 'rev-2',
      compare_to: 'rev-1',
      summary: { added: 0, removed: 0, changed: 1 },
      entries: [
        {
          entity_type: 'event_type',
          kind: 'changed',
          name: 'checkout',
          parent: null,
          changes: ['display_name'],
          field_changes: [{ field: 'display_name', before: 'Checkout', after: 'Checkout flow' }],
        },
      ],
    }
    vi.mocked(planRevisionsApi.diff).mockResolvedValue(diff)
    renderHistory()

    expect(await screen.findByText('Checkout flow')).toBeInTheDocument()
    expect(screen.getByText('Checkout')).toBeInTheDocument()
    expect(screen.getByText('Modified')).toBeInTheDocument()
    expect(screen.queryByText('changed')).not.toBeInTheDocument()
  })
})

describe('HistoryTab — the diff reads aloud', () => {
  it('names before, after and each member change for a screen reader', async () => {
    vi.mocked(planRevisionsApi.list).mockResolvedValue({
      items: [makeRevision({ id: 'rev-2' }), makeRevision({ id: 'rev-1' })],
      total: 2,
    })
    vi.mocked(planRevisionsApi.diff).mockResolvedValue({
      revision_id: 'rev-2',
      compare_to: 'rev-1',
      summary: { added: 0, removed: 0, changed: 1 },
      entries: [
        {
          entity_type: 'event',
          kind: 'changed',
          name: 'checkout',
          parent: null,
          changes: ['display_name', 'field_values'],
          field_changes: [
            { field: 'display_name', before: 'Checkout', after: 'Checkout flow' },
            {
              field: 'field_values',
              before: null,
              after: null,
              items: [{ key: 'currency', kind: 'removed', before: 'USD', after: null }],
            },
          ],
        },
      ],
    })
    renderHistory()

    await screen.findByText('Checkout flow')
    expect(screen.getByText('before:')).toBeInTheDocument()
    expect(screen.getByText('after:')).toBeInTheDocument()
    expect(screen.getByText('removed:')).toBeInTheDocument()
  })
})

describe('HistoryTab — Export JSON Schema (GH #262)', () => {
  const BUNDLE = {
    format: 'jsonschema' as const,
    revision: 'rev-9',
    branch: 'main',
    branch_id: 'b-main',
    plan_hash: 'sha256:abc',
    schemas: {
      'screen_view/home': {
        $schema: 'https://json-schema.org/draft/2020-12/schema',
        type: 'object',
        required: ['screen_type'],
      },
    },
  }

  beforeEach(() => {
    vi.mocked(planExportApi.jsonSchema).mockReset()
    vi.mocked(downloadJson).mockReset()
  })

  it('downloads the bundle for main as a dated .json file', async () => {
    vi.mocked(planExportApi.jsonSchema).mockResolvedValue(BUNDLE)
    renderHistory()

    fireEvent.click(await screen.findByRole('button', { name: /Export JSON Schema/ }))

    await waitFor(() => expect(downloadJson).toHaveBeenCalledTimes(1))
    expect(planExportApi.jsonSchema).toHaveBeenCalledWith(SLUG, null)
    const [filename, data] = vi.mocked(downloadJson).mock.calls[0] ?? []
    expect(filename).toMatch(/^tripl-plan-schema-demo-main-\d{4}-\d{2}-\d{2}\.json$/)
    expect(data).toEqual(BUNDLE)
  })

  it('exports the active branch and names the file after the branch it read', async () => {
    vi.mocked(planExportApi.jsonSchema).mockResolvedValue({
      ...BUNDLE,
      branch: 'feature/checkout',
      branch_id: 'b-9',
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <BranchContext.Provider value={{ branchId: 'b-9', setBranchId: () => {}, slug: SLUG }}>
          <MemoryRouter>
            <HistoryTab slug={SLUG} />
          </MemoryRouter>
        </BranchContext.Provider>
      </QueryClientProvider>,
    )
    await screen.findByText(BRANCH_SUMMARY)

    fireEvent.click(screen.getByRole('button', { name: /Export JSON Schema/ }))

    await waitFor(() => expect(downloadJson).toHaveBeenCalledTimes(1))
    expect(planExportApi.jsonSchema).toHaveBeenCalledWith(SLUG, 'b-9')
    expect(vi.mocked(downloadJson).mock.calls[0]?.[0]).toMatch(
      /^tripl-plan-schema-demo-feature-checkout-\d{4}-\d{2}-\d{2}\.json$/,
    )
  })

  it('says so when the export fails, and downloads nothing', async () => {
    vi.mocked(planExportApi.jsonSchema).mockRejectedValue(new Error('boom'))
    renderHistory()

    fireEvent.click(await screen.findByRole('button', { name: /Export JSON Schema/ }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent("Couldn't export the plan as JSON Schema")
    expect(downloadJson).not.toHaveBeenCalled()
  })
})
