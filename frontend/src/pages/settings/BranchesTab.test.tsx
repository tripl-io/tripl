import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useParams } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { toast } from 'sonner'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { branchSettingsApi } from '@/api/branchSettings'
import { ApiError } from '@/api/client'
import { metaFieldsApi } from '@/api/metaFields'
import { planBranchesApi } from '@/api/planBranches'
import { projectMembersApi } from '@/api/projectMembers'
import { trackerConfigApi } from '@/api/trackerConfig'
import { usersApi } from '@/api/users'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import type {
  ImplementationTicket,
  PlanBranchConflicts,
  PlanBranchDiffSummary,
  PlanBranchSummary,
  ProjectBranchSettings,
  ProjectTrackerConfig,
  Role,
  UserListItem,
} from '@/types'
import { BranchProvider } from '@/components/branch-context'
import { useActiveBranchId } from '@/hooks/useBranch'
import { BranchesTab } from './BranchesTab'
import { expectNoAxeViolations } from '@/test/axe'
import { at } from '@/test/at'
import { personaAuth, type Persona } from '@/test/persona'
import { PersonaProject } from '@/test/PersonaProject'

vi.mock('@/api/planBranches', () => ({
  planBranchesApi: {
    list: vi.fn(),
    get: vi.fn(),
    diff: vi.fn(),
    getConflicts: vi.fn(),
    getUpdatePreview: vi.fn(),
    updateFromMain: vi.fn(),
    listComments: vi.fn(),
    create: vi.fn(),
    delete: vi.fn(),
    transition: vi.fn(),
    merge: vi.fn(),
    createComment: vi.fn(),
    saveResolution: vi.fn(),
    revert: vi.fn(),
    listImplementationTickets: vi.fn(),
    addReviewer: vi.fn(),
    removeReviewer: vi.fn(),
  },
}))

vi.mock('@/api/trackerConfig', () => ({
  trackerConfigApi: {
    get: vi.fn(),
    update: vi.fn(),
  },
}))

vi.mock('@/api/branchSettings', () => ({
  branchSettingsApi: {
    get: vi.fn(),
    update: vi.fn(),
  },
}))

vi.mock('@/api/users', () => ({
  usersApi: {
    list: vi.fn(),
    updateRole: vi.fn(),
  },
}))

vi.mock('@/api/projectMembers', () => ({
  projectMembersApi: {
    list: vi.fn(),
    add: vi.fn(),
    updateRole: vi.fn(),
    remove: vi.fn(),
  },
}))

vi.mock('@/api/metaFields', () => ({
  metaFieldsApi: {
    list: vi.fn(),
  },
}))

function makeUser(overrides: Partial<UserListItem>): UserListItem {
  return {
    id: 'u-1',
    email: 'user@example.com',
    name: null,
    role: 'member',
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

// created_by arrives from the API as a bare user id; the tab resolves it
// against this roster (name, falling back to email).
const USERS = [
  makeUser({ id: 'u-maya', name: 'Maya R.', email: 'maya@example.com' }),
  makeUser({ id: 'u-priya', name: 'Priya S.', email: 'priya@example.com' }),
]

function makeSettings(overrides: Partial<ProjectBranchSettings>): ProjectBranchSettings {
  return {
    id: 's-1',
    project_id: 'p-1',
    min_approvals: 1,
    block_self_approval: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeBranch(overrides: Partial<PlanBranchSummary>): PlanBranchSummary {
  return {
    id: 'b-1',
    project_id: 'p-1',
    name: 'main',
    kind: 'main',
    status: 'merged',
    description: '',
    base_revision_id: null,
    created_by: null,
    merged_at: null,
    merged_by: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

const MAIN = makeBranch({ id: 'main-1', name: 'main', kind: 'main', status: 'merged' })
const FEATURE = makeBranch({
  id: 'feat-1',
  name: 'checkout-v2',
  kind: 'working',
  status: 'approved',
  created_by: 'u-maya',
})
const MERGED = makeBranch({
  id: 'feat-merged',
  name: 'checkout-v3',
  kind: 'working',
  status: 'merged',
  created_by: 'u-maya',
  merged_at: '2026-02-01T00:00:00Z',
  merged_by: 'u-maya',
})

function makeTrackerConfig(overrides: Partial<ProjectTrackerConfig> = {}): ProjectTrackerConfig {
  return {
    id: null,
    project_id: 'p-1',
    enabled: false,
    tracker_type: 'jira',
    base_url: '',
    project_key: '',
    auth_email: '',
    issue_type: 'Task',
    api_token_set: false,
    created_at: null,
    updated_at: null,
    ...overrides,
  }
}

/** Merge to main stays disabled until the selected branch's diff has loaded
 * (the confirm reads it), and the diff is no longer prefetched by a per-row
 * fan-out — so wait for the button to come live before clicking. */
async function clickMerge() {
  const button = await screen.findByRole('button', { name: /Merge to main/i })
  await waitFor(() => expect(button).toBeEnabled())
  fireEvent.click(button)
}

/** Every merge now asks first: the plain case's confirm is "Merge". */
async function confirmMerge() {
  fireEvent.click(await screen.findByRole('button', { name: 'Merge' }))
}

function makeTicket(overrides: Partial<ImplementationTicket> = {}): ImplementationTicket {
  return {
    id: 't-1',
    project_id: 'p-1',
    branch_id: 'feat-merged',
    tracker_type: 'jira',
    external_id: '10042',
    external_key: 'ENG-42',
    external_url: 'https://example.atlassian.net/browse/ENG-42',
    status: 'open',
    summary: 'Implement checkout-v3',
    event_ids: ['e-1'],
    created_at: '2026-02-01T00:00:00Z',
    updated_at: '2026-02-01T00:00:00Z',
    closed_at: null,
    ...overrides,
  }
}

/** The four queries a feature-branch detail always fires; ticket-focused tests
 * only care about the fifth. */
function mockBranchDetailQueries(items: PlanBranchSummary[]) {
  vi.mocked(planBranchesApi.list).mockResolvedValue({ items, total: items.length })
  vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
  vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
  vi.mocked(planBranchesApi.diff).mockResolvedValue({
    behind_base: false,
    summary: { added: 0, removed: 0, changed: 0 },
    entries: [],
  })
}

/** The selected branch comes from the route, so the tab is mounted behind the
 * real `/p/:slug/branches/:branchId` routes — selecting a branch in the
 * list navigates, exactly as it does in the app. */
function BranchesTabRoute() {
  const { branchId } = useParams<{ branchId?: string }>()
  return <BranchesTab slug="demo" branchId={branchId} />
}

function authAs(role: Role): AuthContextValue {
  return {
    user: {
      id: `${role}-1`,
      email: `${role}@example.com`,
      name: role,
      role,
      is_platform_admin: false,
      orgs: [],
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
    status: 'authenticated',
    error: null,
    isLoggingOut: false,
    logout: async () => {},
    refresh: () => {},
  }
}

/** Rendered as an owner unless a test says otherwise: the merge policy form is
 * owner-only, and most tests here exercise the full set of actions. */
function renderTab(branchId?: string, role: Persona = 'owner') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const path = `/p/demo/branches${branchId ? `/${branchId}` : ''}`
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={personaAuth(role)}>
        <PersonaProject persona={role}>
          <MemoryRouter initialEntries={[path]}>
            <Routes>
              <Route path="/p/:slug/branches" element={<BranchesTabRoute />} />
              <Route path="/p/:slug/branches/:branchId" element={<BranchesTabRoute />} />
            </Routes>
          </MemoryRouter>
        </PersonaProject>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.mocked(usersApi.list).mockResolvedValue(USERS)
  // The reviewer picker offers the project's members, not the whole roster;
  // here everyone on the roster is a member.
  vi.mocked(projectMembersApi.list).mockResolvedValue(
    USERS.map((u) => ({
      user_id: u.id,
      name: u.name,
      email: u.email,
      role: 'editor' as const,
      added_at: '2026-01-01T00:00:00Z',
    })),
  )
  vi.mocked(metaFieldsApi.list).mockResolvedValue([])
  // Every query the tab can issue answers with a valid, empty payload unless a
  // test says otherwise. A bare vi.fn() resolves to undefined, which react-query
  // turns into an error — so the panels a test does not look at used to run in
  // their error state, and a regression there could not show.
  vi.mocked(branchSettingsApi.get).mockResolvedValue(makeSettings({}))
  vi.mocked(planBranchesApi.get).mockImplementation(async (_slug, branchId) => ({
    ...([MAIN, FEATURE, MERGED].find((branch) => branch.id === branchId) ??
      makeBranch({ id: branchId, name: branchId, kind: 'working', status: 'draft' })),
    reviewers: [],
    approvals: [],
  }))
  vi.mocked(planBranchesApi.diff).mockResolvedValue({
    entries: [],
    summary: { added: 0, removed: 0, changed: 0 },
    behind_base: false,
  })
  vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
  vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
  vi.mocked(planBranchesApi.listImplementationTickets).mockResolvedValue([])
  vi.mocked(trackerConfigApi.get).mockResolvedValue(makeTrackerConfig())
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('BranchesTab', () => {
  it('renders the page head and a branches list panel', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })

    renderTab()

    expect(await screen.findByText('Plan branches')).toBeInTheDocument()
    expect(
      screen.getByText('version control for your schema', { exact: false }),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /New branch/i })).toBeInTheDocument()
    // List row renders once the branches query resolves.
    expect(await screen.findByText('checkout-v2')).toBeInTheDocument()
    // "main" appears in the list row and the detail header.
    expect(screen.getAllByText('main').length).toBeGreaterThan(0)
    expect(screen.getByText('production')).toBeInTheDocument()
  })

  it('keeps merged branches out of the active list, behind their own tab', async () => {
    mockBranchDetailQueries([MAIN, FEATURE, MERGED])

    renderTab()

    // Active by default: the in-flight branch is listed, the merged one is not.
    // Scoped to the list: main's pane names the last merged branch.
    expect(await screen.findByText('checkout-v2')).toBeInTheDocument()
    const list = screen.getByRole('region', { name: 'Branches' })
    expect(within(list).queryByText('checkout-v3')).not.toBeInTheDocument()
    // main is status 'merged' but kind 'main' — it must stay on the active tab,
    // or the base branch disappears from the list a status-only filter produces.
    expect(screen.getAllByText('main').length).toBeGreaterThan(0)

    // Counts are on the tabs themselves: main is listed but is not open work,
    // so one open branch and one closed.
    expect(screen.getByRole('button', { name: 'Open 1' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Closed 1' }))

    expect(await within(list).findByText('checkout-v3')).toBeInTheDocument()
    expect(within(list).queryByText('checkout-v2')).not.toBeInTheDocument()
  })

  it('opens on the merged tab when the route points at a merged branch', async () => {
    mockBranchDetailQueries([MAIN, FEATURE, MERGED])

    // A deep link (a bookmark, or an alert's "view branch") must not land on a
    // tab that cannot show the branch it selected.
    renderTab('feat-merged')

    // Two matches on purpose: the list row and the detail header — which is
    // itself the proof the row is rendered rather than only the detail pane.
    expect((await screen.findAllByText('checkout-v3')).length).toBeGreaterThan(1)
    expect(screen.getByRole('button', { name: 'Closed 1' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  it('resolves the creator id to the user name in the list row and detail header', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 0 },
      entries: [],
    })

    renderTab()

    // List row subtitle shows the resolved name, never the raw user id.
    expect(await screen.findByText(/Maya R\. ·/)).toBeInTheDocument()

    fireEvent.click(await screen.findByText('checkout-v2'))
    expect(await screen.findByText(/Opened by Maya R\. ·/)).toBeInTheDocument()
    expect(screen.queryByText(/u-maya/)).not.toBeInTheDocument()
  })

  it('falls back to the user email when the creator has no name', async () => {
    vi.mocked(usersApi.list).mockResolvedValue([
      makeUser({ id: 'u-mail', email: 'dev@example.com', name: null }),
    ])
    const feature = makeBranch({
      id: 'feat-3',
      name: 'email-only',
      kind: 'working',
      status: 'draft',
      created_by: 'u-mail',
    })
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, feature], total: 2 })

    renderTab()

    expect(await screen.findByText(/dev@example\.com ·/)).toBeInTheDocument()
  })

  it("shows 'unknown' when the creator is missing from the roster", async () => {
    const feature = makeBranch({
      id: 'feat-4',
      name: 'orphaned',
      kind: 'working',
      status: 'draft',
      created_by: 'u-gone',
    })
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, feature], total: 2 })

    renderTab()

    expect(await screen.findByText(/unknown ·/)).toBeInTheDocument()
    expect(screen.queryByText(/u-gone/)).not.toBeInTheDocument()
  })

  it('shows the default-branch empty state when main is selected', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })

    renderTab()

    expect(await screen.findByText(/every change merges here/i)).toBeInTheDocument()
  })

  it('renders the real diff with tone-coded counts and per-change rows for a feature branch', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: true,
      summary: { added: 2, removed: 1, changed: 1 },
      entries: [
        {
          entity_type: 'event',
          kind: 'added',
          name: 'checkout_address_autofilled',
          parent: null,
          changes: ['New event'],
          field_changes: [],
          before: null,
          after: { name: 'checkout_address_autofilled', status: 'active' },
        },
        {
          entity_type: 'event',
          kind: 'changed',
          name: 'payment_failed',
          parent: null,
          changes: ['Added field error_code'],
          field_changes: [],
          before: null,
          after: null,
        },
        {
          entity_type: 'event',
          kind: 'removed',
          name: 'promo_code_invalid',
          parent: null,
          changes: [],
          field_changes: [],
          before: { name: 'promo_code_invalid', status: 'deprecated' },
          after: null,
        },
      ],
    })

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))

    await waitFor(() => expect(planBranchesApi.diff).toHaveBeenCalledWith('demo', 'feat-1'))
    expect(await screen.findByText('+2')).toBeInTheDocument()
    expect(screen.getByText('~1')).toBeInTheDocument()
    expect(screen.getByText('−1')).toBeInTheDocument()
    // A yes/no, not "↓ 1 behind". This conflicts answer carries no
    // overlap count, so the note never calls the merge safe.
    expect(screen.getByText(/Main has newer changes since this branch was created/)).toBeInTheDocument()
    expect(screen.queryByText(/safe to merge/)).not.toBeInTheDocument()

    expect(screen.getByText('checkout_address_autofilled')).toBeInTheDocument()
    expect(screen.getByText('payment_failed')).toBeInTheDocument()
    expect(screen.getByText('Added')).toBeInTheDocument()
    expect(screen.getByText('Modified')).toBeInTheDocument()
    expect(screen.getByText('Removed')).toBeInTheDocument()
  })

  it('heads a single-change branch "1 change", not "1 changes"', async () => {
    // A branch that renames one field is the ordinary case, not a corner: the
    // Changes panel is the first thing a reviewer opens, and it read
    // "Changes / 1 changes". Same defect the Scans list shipped as "1 scans"
    // — `countOf` exists so this is not the fourth hand-rolled copy.
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 1 },
      entries: [
        {
          entity_type: 'event',
          kind: 'changed',
          name: 'payment_failed',
          parent: null,
          changes: ['Added field error_code'],
          field_changes: [],
          before: null,
          after: null,
        },
      ],
    })

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))
    await waitFor(() => expect(planBranchesApi.diff).toHaveBeenCalledWith('demo', 'feat-1'))

    expect(await screen.findByText('1 change')).toBeInTheDocument()
    expect(screen.queryByText('1 changes')).not.toBeInTheDocument()
  })

  it('selects the branch named in the route without a click', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 0 },
      entries: [],
    })

    // A shared link lands straight on the branch's review — no main detour.
    renderTab('feat-1')

    await waitFor(() => expect(planBranchesApi.diff).toHaveBeenCalledWith('demo', 'feat-1'))
    expect(await screen.findByText('No changes in this branch.')).toBeInTheDocument()
    expect(screen.queryByText(/every change merges here/i)).not.toBeInTheDocument()
  })

  it('renders the comment author name instead of an anonymous comment', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 0 },
      entries: [],
    })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([
      {
        id: 'comment-1',
        branch_id: 'feat-1',
        parent_id: null,
        user_id: 'u-priya',
        body: 'Please rename this event.',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
      },
    ])

    renderTab('feat-1')

    expect(await screen.findByText('Please rename this event.')).toBeInTheDocument()
    expect(await screen.findByText(/Priya S\./)).toBeInTheDocument()
  })

  it('answers a review remark in place instead of starting a second one', async () => {
    // PlanBranchComment has carried parent_id all along and the service has
    // validated it; the panel simply never posted one.
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 0 },
      entries: [],
    })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([
      {
        id: 'comment-1',
        branch_id: 'feat-1',
        parent_id: null,
        user_id: 'u-priya',
        body: 'Please rename this event.',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
      },
    ])
    vi.mocked(planBranchesApi.createComment).mockResolvedValue({} as never)

    renderTab('feat-1')

    fireEvent.click(await screen.findByRole('button', { name: 'reply' }))
    fireEvent.change(screen.getByLabelText('Write a comment'), {
      target: { value: 'Renamed on the branch.' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Reply' }))

    await waitFor(() =>
      expect(planBranchesApi.createComment).toHaveBeenCalledWith(
        'demo',
        'feat-1',
        'Renamed on the branch.',
        'comment-1',
      ),
    )
  })

  it('breaks a changed collection down per member instead of dumping JSON', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 1 },
      entries: [
        {
          entity_type: 'event',
          kind: 'changed',
          name: 'purchase',
          parent: 'track',
          entity_id: 'ev-1',
          changes: ['field_values: 1 added, 1 changed'],
          field_changes: [
            {
              field: 'field_values',
              before: [{ field_name: 'currency', value: 'USD', is_authored: true }],
              after: [
                { field_name: 'currency', value: 'EUR', is_authored: true },
                { field_name: 'method', value: 'card', is_authored: true },
              ],
              items: [
                {
                  key: 'currency',
                  kind: 'changed',
                  before: { value: 'USD', is_authored: true },
                  after: { value: 'EUR', is_authored: true },
                },
                {
                  key: 'method',
                  kind: 'added',
                  before: null,
                  after: { value: 'card', is_authored: true },
                },
              ],
            },
          ],
          before: null,
          after: null,
        },
      ],
    })

    renderTab('feat-1')

    fireEvent.click(await screen.findByRole('button', { name: /purchase/i }))

    // Each changed member is named and shown with its own before → after …
    expect(await screen.findByText('currency')).toBeInTheDocument()
    expect(screen.getByText('method')).toBeInTheDocument()
    expect(screen.getByText(/value: USD/)).toBeInTheDocument()
    expect(screen.getByText(/value: EUR/)).toBeInTheDocument()
    // … and no JSON blob of the whole collection is rendered.
    expect(screen.queryByText(/"field_name"/)).not.toBeInTheDocument()
  })

  it('reverts a single changed field after the reviewer confirms', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 1 },
      entries: [
        {
          entity_type: 'event',
          kind: 'changed',
          name: 'purchase',
          parent: 'track',
          entity_id: 'ev-1',
          changes: ["description: '' → 'edited'"],
          field_changes: [{ field: 'description', before: '', after: 'edited', items: [] }],
          before: null,
          after: null,
        },
      ],
    })
    vi.mocked(planBranchesApi.revert).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 0 },
      entries: [],
    })

    renderTab('feat-1')

    fireEvent.click(await screen.findByRole('button', { name: /purchase/i }))
    fireEvent.click(await screen.findByRole('button', { name: /Revert description/i }))

    // Reverting is destructive to branch work, so it waits for consent.
    expect(planBranchesApi.revert).not.toHaveBeenCalled()
    fireEvent.click(await screen.findByRole('button', { name: 'Revert' }))

    await waitFor(() =>
      expect(planBranchesApi.revert).toHaveBeenCalledWith('demo', 'feat-1', {
        entity_type: 'event',
        name: 'purchase',
        parent: 'track',
        field: 'description',
        entity_id: 'ev-1',
      }),
    )
  })

  it('restores an entity the branch deleted', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 1, changed: 0 },
      entries: [
        {
          entity_type: 'event',
          kind: 'removed',
          name: 'legacy_event',
          parent: 'track',
          entity_id: 'ev-old',
          changes: [],
          field_changes: [],
          before: { name: 'legacy_event' },
          after: null,
        },
      ],
    })

    vi.mocked(planBranchesApi.revert).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 0 },
      entries: [],
    })

    renderTab('feat-1')

    fireEvent.click(await screen.findByRole('button', { name: /legacy_event/i }))

    // The row links to the copy that survives on main — the branch has none.
    expect(await screen.findByRole('link', { name: /Open on main/i })).toHaveAttribute(
      'href',
      '/p/demo/events/all/ev-old',
    )

    fireEvent.click(screen.getByRole('button', { name: /Restore on this branch/i }))
    fireEvent.click(await screen.findByRole('button', { name: 'Restore' }))

    await waitFor(() =>
      expect(planBranchesApi.revert).toHaveBeenCalledWith('demo', 'feat-1', {
        entity_type: 'event',
        name: 'legacy_event',
        parent: 'track',
        field: null,
        entity_id: 'ev-old',
      }),
    )
  })

  it('offers authoring on the branch you are reviewing, and not on a merged one', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 0 },
      entries: [],
    })

    const { unmount } = renderTab('feat-1')

    expect(await screen.findByRole('link', { name: 'New event on this branch' })).toHaveAttribute(
      'href',
      '/p/demo/events/all/new?branch=feat-1',
    )
    // One explicit "work on this branch" action, which carries the branch.
    expect(screen.getByRole('link', { name: 'Work on this branch' })).toHaveAttribute(
      'href',
      '/p/demo/events?branch=feat-1',
    )
    unmount()

    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, MERGED], total: 2 })
    vi.mocked(planBranchesApi.listImplementationTickets).mockResolvedValue([])
    renderTab('feat-merged')

    await screen.findByText('No changes in this branch.')
    expect(screen.queryByRole('link', { name: 'New event on this branch' })).not.toBeInTheDocument()
  })

  it('reads a created event as a table, not as middot-joined prose', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 1, removed: 0, changed: 0 },
      entries: [
        {
          entity_type: 'event',
          kind: 'added',
          name: 'se_spot_forecast_profile_confirm',
          parent: 'se',
          entity_id: 'ev-9',
          changes: [],
          // An event created on a branch has no field_changes at all, so the
          // full state below is its whole detail view.
          field_changes: [],
          before: null,
          after: {
            name: 'se_spot_forecast_profile_confirm',
            field_values: [
              { field_name: 'action', value: 'confirm', is_authored: true },
              { field_name: 'category', value: 'spot', is_authored: false },
              {
                field_name: 'property',
                value:
                  '{"from_profile": "${property.forecast_profile}", "mode": "${property.mode}"}',
                is_authored: true,
              },
            ],
          },
        },
      ],
    })

    renderTab('feat-1')

    fireEvent.click(await screen.findByRole('button', { name: /se_spot_forecast_profile_confirm/i }))

    const table = await screen.findByRole('table')
    expect(within(table).getByRole('columnheader', { name: 'Field' })).toBeInTheDocument()
    expect(within(table).getByRole('columnheader', { name: 'Value' })).toBeInTheDocument()
    // is_authored is not a column: the backend already rules a flip of it out of
    // change detection. It shows only where a scan, not a person, wrote the row.
    expect(within(table).queryByRole('columnheader', { name: /authored/i })).not.toBeInTheDocument()
    expect(within(table).getByText('from scan')).toBeInTheDocument()
    expect(screen.queryByText(/field_name: action/)).not.toBeInTheDocument()

    // The JSON payload is the value that blew the line out; it collapses to a
    // preview with the pretty-printed form one click away.
    const payload = within(table).getByRole('button', { name: /from_profile/ })
    expect(payload).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(payload)
    expect(payload).toHaveAttribute('aria-expanded', 'true')
    const pretty = payload.parentElement?.querySelector('pre')
    expect(pretty?.textContent).toContain('"mode": "${property.mode}"')
    expect(pretty?.textContent).toContain('\n')
  })

  it('offers Edit on the collapsed row, straight to the editor on this branch', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 1, removed: 0, changed: 0 },
      entries: [
        {
          entity_type: 'event',
          kind: 'added',
          name: 'checkout_started',
          parent: 'track',
          entity_id: 'ev-9',
          changes: [],
          field_changes: [],
          before: null,
          after: { name: 'checkout_started', status: 'active' },
        },
        {
          entity_type: 'variable',
          kind: 'added',
          name: 'variant',
          parent: null,
          entity_id: 'var-3',
          changes: [],
          field_changes: [],
          before: null,
          after: { name: 'variant' },
        },
        {
          entity_type: 'field_definition',
          kind: 'added',
          name: 'order_id',
          parent: 'track',
          entity_id: 'fd-2',
          changes: [],
          field_changes: [],
          before: null,
          after: { name: 'order_id' },
        },
      ],
    })

    renderTab('feat-1')

    // No expansion first: the shortcut is the point.
    const edit = await screen.findByRole('link', { name: 'Edit checkout_started' })
    expect(edit).toHaveAttribute('href', '/p/demo/events/all/ev-9/edit?branch=feat-1')

    // A variable's Edit opens its own page, Definition tab first.
    // Before this it had no Edit at all and a reviewer had to
    // expand the row and find the small link after Revert.
    const editVariable = await screen.findByRole('link', { name: 'Edit variant' })
    expect(editVariable).toHaveAttribute(
      'href',
      '/p/demo/variables/var-3?branch=feat-1',
    )

    // Field definitions still have no editor to point at, so the row must not
    // grow a link that goes nowhere.
    expect(screen.queryByRole('link', { name: 'Edit order_id' })).not.toBeInTheDocument()
  })

  it('edits the branch-side copy of a renamed event, not the base-side id the row carries', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 1, removed: 1, changed: 0 },
      renames: [
        {
          entity_type: 'event',
          parent: 'track',
          removed_name: 'checkout_start',
          added_name: 'checkout_started',
        },
      ],
      entries: [
        {
          entity_type: 'event',
          kind: 'removed',
          name: 'checkout_start',
          parent: 'track',
          entity_id: 'ev-base',
          changes: [],
          field_changes: [],
          before: { name: 'checkout_start' },
          after: null,
        },
        {
          entity_type: 'event',
          kind: 'added',
          name: 'checkout_started',
          parent: 'track',
          entity_id: 'ev-branch',
          changes: [],
          field_changes: [],
          before: null,
          after: { name: 'checkout_started' },
        },
      ],
    })

    renderTab('feat-1')

    const edit = await screen.findByRole('link', { name: 'Edit checkout_started' })
    expect(edit).toHaveAttribute('href', '/p/demo/events/all/ev-branch/edit?branch=feat-1')
  })

  it('does not offer Edit on a merged branch, whose writes nothing would refuse', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, MERGED], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.listImplementationTickets).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 1, removed: 0, changed: 0 },
      entries: [
        {
          entity_type: 'event',
          kind: 'added',
          name: 'checkout_started',
          parent: 'track',
          entity_id: 'ev-9',
          changes: [],
          field_changes: [],
          before: null,
          after: { name: 'checkout_started' },
        },
      ],
    })

    renderTab('feat-merged')

    await screen.findByRole('button', { name: /checkout_started/i })
    expect(screen.queryByRole('link', { name: /^Edit / })).not.toBeInTheDocument()
  })

  it('links a diff row to the entity it describes, in the branch', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 1, removed: 0, changed: 0 },
      entries: [
        {
          entity_type: 'event',
          kind: 'added',
          name: 'checkout_started',
          parent: 'track',
          entity_id: 'ev-9',
          changes: [],
          field_changes: [],
          before: null,
          after: { name: 'checkout_started', status: 'active' },
        },
      ],
    })

    renderTab('feat-1')

    fireEvent.click(await screen.findByRole('button', { name: /checkout_started/i }))

    const link = await screen.findByRole('link', { name: /Open event/i })
    // The event lives on the branch, so the link opens it there.
    expect(link).toHaveAttribute('href', '/p/demo/events/all/ev-9?branch=feat-1')
  })

  it('expands a change row to reveal the field diff and full state', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 1 },
      entries: [
        {
          entity_type: 'variable',
          kind: 'changed',
          name: 'user_plan',
          parent: null,
          changes: ["variable_type: 'string' → 'enum'"],
          field_changes: [{ field: 'variable_type', before: 'string', after: 'enum' }],
          before: { name: 'user_plan', variable_type: 'string' },
          after: { name: 'user_plan', variable_type: 'enum' },
        },
      ],
    })

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))

    // The row is a collapsed, clickable disclosure until the user opens it.
    const row = await screen.findByRole('button', { name: /user_plan/i })
    expect(row).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('Full state')).not.toBeInTheDocument()

    fireEvent.click(row)

    expect(row).toHaveAttribute('aria-expanded', 'true')
    // The field-level diff surfaces once expanded; for a modification the full
    // state is one more click, so it no longer dominates the row.
    expect(await screen.findByText('Field changes')).toBeInTheDocument()
    expect(screen.queryByText('Full state')).not.toBeInTheDocument()
    const fullToggle = screen.getByRole('button', { name: 'Show full property (2 properties)' })
    expect(fullToggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(fullToggle)
    expect(screen.getByText('Full state')).toBeInTheDocument()
    // The same toggle closes it again.
    expect(fullToggle).toHaveAttribute('aria-expanded', 'true')
    expect(fullToggle).toHaveAccessibleName('Hide full property (2 properties)')
    // Keys read as labels, with the raw key kept in the title.
    expect(screen.getByText('Property type')).toHaveAttribute('title', 'variable_type')
    // The before value of the changed field renders (the 'string' → 'enum' move).
    expect(screen.getByText('string')).toBeInTheDocument()
    expect(screen.getAllByText('variable_type').length).toBeGreaterThan(0)
    // The collapsed summary names the field in words, not its key.
    expect(screen.getByText('Property type string → enum')).toBeInTheDocument()

    fireEvent.click(fullToggle)
    expect(fullToggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('Full state')).not.toBeInTheDocument()

    // Clicking again collapses the detail.
    fireEvent.click(row)
    expect(row).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('Full state')).not.toBeInTheDocument()
  })

  it('preserves the merge workflow: approved branch exposes Merge to main', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 0 },
      entries: [],
    })
    vi.mocked(planBranchesApi.merge).mockResolvedValue({} as never)

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))
    expect(await screen.findByText('No changes in this branch.')).toBeInTheDocument()
    expect(screen.getByText('0 changes')).toBeInTheDocument()
    const mergeBtn = await screen.findByRole('button', { name: /Merge to main/i })
    fireEvent.click(mergeBtn)
    // Never one click: the confirm names what lands on main.
    expect(await screen.findByText(/Merge 0 changes into main\?/)).toBeInTheDocument()
    expect(planBranchesApi.merge).not.toHaveBeenCalled()
    await confirmMerge()
    await waitFor(() => expect(planBranchesApi.merge).toHaveBeenCalledWith('demo', 'feat-1'))
  })

  it('switches the shell back to main when the active branch is merged', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 0 },
      entries: [],
    })
    vi.mocked(planBranchesApi.merge).mockResolvedValue({} as never)
    localStorage.setItem('tripl-branch:demo', FEATURE.id)

    function ActiveBranch() {
      return <output aria-label="active branch">{useActiveBranchId() ?? 'main'}</output>
    }
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={authAs('owner')}>
          <MemoryRouter initialEntries={[`/p/demo/branches/${FEATURE.id}`]}>
            <BranchProvider slug="demo">
              <ActiveBranch />
              <Routes>
                <Route path="/p/:slug/branches/:branchId" element={<BranchesTabRoute />} />
              </Routes>
            </BranchProvider>
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )
    const active = screen.getByRole('status', { name: 'active branch' })
    expect(active).toHaveTextContent(FEATURE.id)

    await clickMerge()
    await confirmMerge()
    await waitFor(() => expect(active).toHaveTextContent('main'))
    expect(localStorage.getItem('tripl-branch:demo')).toBeNull()
  })

  it('preserves the transition workflow for non-approved statuses', async () => {
    const draftFeature = makeBranch({
      id: 'feat-2',
      name: 'gdpr-audit',
      kind: 'working',
      status: 'draft',
      created_by: 'u-priya',
    })
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, draftFeature], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 1, removed: 0, changed: 0 },
      entries: [],
    })
    vi.mocked(planBranchesApi.transition).mockResolvedValue({} as never)

    renderTab()

    fireEvent.click(await screen.findByText('gdpr-audit'))
    const submitBtn = await screen.findByRole('button', { name: 'Submit for review' })
    // The roster has loaded (the author's name resolves), so Submit on an
    // unstaffed branch asks who should review it first.
    await screen.findByText(/Opened by Priya S\./)
    fireEvent.click(submitBtn)
    expect(await screen.findByLabelText('Who should review this?')).toBeInTheDocument()
    expect(planBranchesApi.transition).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Submit without a reviewer' }))
    await waitFor(() =>
      expect(planBranchesApi.transition).toHaveBeenCalledWith('demo', 'feat-2', 'submit'),
    )
    expect(screen.queryByRole('button', { name: /Merge to main/i })).not.toBeInTheDocument()
  })

  it('adds the reviewer picked on Submit, then submits', async () => {
    const draftFeature = makeBranch({
      id: 'feat-2',
      name: 'gdpr-audit',
      kind: 'working',
      status: 'draft',
      created_by: 'u-priya',
    })
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, draftFeature], total: 2 })
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 1, removed: 0, changed: 0 },
      entries: [],
    })
    vi.mocked(planBranchesApi.addReviewer).mockResolvedValue({
      id: 'r-1',
      user_id: 'u-maya',
      created_at: '2026-01-03T00:00:00Z',
    })
    vi.mocked(planBranchesApi.transition).mockResolvedValue({} as never)

    renderTab('feat-2')

    const submitBtn = await screen.findByRole('button', { name: 'Submit for review' })
    await screen.findByText(/Opened by Priya S\./)
    fireEvent.click(submitBtn)
    const picker = await screen.findByLabelText('Who should review this?')
    expect(picker).toHaveFocus()
    // Cancel closes the prompt and sends nothing.
    fireEvent.click(within(picker.closest('form')!).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByLabelText('Who should review this?')).not.toBeInTheDocument()
    expect(planBranchesApi.transition).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Submit for review' }))
    fireEvent.change(await screen.findByLabelText('Who should review this?'), {
      target: { value: 'u-maya' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Add and submit' }))
    await waitFor(() =>
      expect(planBranchesApi.transition).toHaveBeenCalledWith('demo', 'feat-2', 'submit'),
    )
    expect(planBranchesApi.addReviewer).toHaveBeenCalledWith('demo', 'feat-2', 'u-maya')
  })

  it('submits at once when the branch already has a reviewer', async () => {
    const draftFeature = makeBranch({
      id: 'feat-2',
      name: 'gdpr-audit',
      kind: 'working',
      status: 'draft',
      created_by: 'u-priya',
    })
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, draftFeature], total: 2 })
    vi.mocked(planBranchesApi.get).mockResolvedValue({
      ...draftFeature,
      reviewers: [{ id: 'r-1', user_id: 'u-maya', created_at: '2026-01-01T00:00:00Z' }],
      approvals: [],
    })
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 1, removed: 0, changed: 0 },
      entries: [],
    })
    vi.mocked(planBranchesApi.transition).mockResolvedValue({} as never)

    renderTab('feat-2')

    await screen.findByRole('button', { name: 'Remove reviewer Maya R.' })
    fireEvent.click(screen.getByRole('button', { name: 'Submit for review' }))
    await waitFor(() =>
      expect(planBranchesApi.transition).toHaveBeenCalledWith('demo', 'feat-2', 'submit'),
    )
    expect(screen.queryByLabelText('Who should review this?')).not.toBeInTheDocument()
  })

  it('opens the create dialog from the New branch button', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN], total: 1 })

    renderTab()

    fireEvent.click(await screen.findByRole('button', { name: /New branch/i }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('New branch')).toBeInTheDocument()
    expect(within(dialog).getByPlaceholderText(/checkout\/paywall-copy/i)).toBeInTheDocument()
    // What a branch is, before anyone creates one.
    expect(within(dialog).getByText(/private copy of the plan/)).toBeInTheDocument()
    expect(within(dialog).getByLabelText('Switch to this branch now')).toBeChecked()
  })

  it('opens the merge policy dialog and saves the settings', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN], total: 1 })
    vi.mocked(branchSettingsApi.get).mockResolvedValue(
      makeSettings({ min_approvals: 1, block_self_approval: false }),
    )
    vi.mocked(branchSettingsApi.update).mockResolvedValue(
      makeSettings({ min_approvals: 2, block_self_approval: true }),
    )

    renderTab()

    fireEvent.click(await screen.findByRole('button', { name: /Merge policy/i }))
    const dialog = await screen.findByRole('dialog')
    const input = await within(dialog).findByLabelText('Required approvals')
    await waitFor(() => expect(input).toHaveValue(1))

    fireEvent.change(input, { target: { value: '2' } })
    fireEvent.click(within(dialog).getByLabelText('Block self-approval'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(branchSettingsApi.update).toHaveBeenCalledWith('demo', {
        min_approvals: 2,
        block_self_approval: true,
      }),
    )
  })

  it('shows an editor the merge policy read-only, as the owner-only PATCH requires', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN], total: 1 })
    renderTab(undefined, 'member')

    fireEvent.click(await screen.findByRole('button', { name: /Merge policy/i }))
    const dialog = await screen.findByRole('dialog')
    // Values to read, not a form of disabled controls (#237).
    expect(await within(dialog).findByText('Only an owner can change the merge policy.')).toBeInTheDocument()
    expect(within(dialog).getByText('Required approvals')).toBeInTheDocument()
    expect(within(dialog).queryByRole('spinbutton')).not.toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
  })

  it('offers a viewer no branch actions', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    renderTab(FEATURE.id, 'viewer')

    expect(await screen.findByRole('note')).toHaveTextContent(/viewer role/)
    await screen.findByRole('link', { name: 'View events on this branch' })
    expect(screen.queryByRole('button', { name: /New branch/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'New event on this branch' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete branch' })).not.toBeInTheDocument()
    // Anchored: a list row's name carries its "Approved" status chip.
    expect(
      screen.queryByRole('button', { name: /^(Approve|Request changes|Merge to main)\b/ }),
    ).not.toBeInTheDocument()
  })

  it('shows the approvals chip against the required quota', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 0 },
      entries: [],
    })
    vi.mocked(planBranchesApi.get).mockResolvedValue({
      ...FEATURE,
      reviewers: [],
      approvals: [{ user_id: 'u-1', approved_at: '2026-01-01T00:00:00Z', stale: false }],
    })
    vi.mocked(branchSettingsApi.get).mockResolvedValue(makeSettings({ min_approvals: 2 }))

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))
    expect(await screen.findByText('Approvals 1/2')).toBeInTheDocument()
  })

  // The bug this pins: the chip used to count approval ROWS, so this exact
  // fixture rendered a green "Approvals 1/1" while the merge endpoint scored
  // the branch at current=0 and refused with insufficient_approvals. The chip
  // has to agree with the gate, or the Approve button looks broken.
  it('excludes a stale approval from the quota and labels it', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 0 },
      entries: [],
    })
    vi.mocked(planBranchesApi.get).mockResolvedValue({
      ...FEATURE,
      reviewers: [],
      approvals: [{ user_id: 'u-1', approved_at: '2026-01-01T00:00:00Z', stale: true }],
    })
    vi.mocked(branchSettingsApi.get).mockResolvedValue(makeSettings({ min_approvals: 1 }))

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))
    expect(await screen.findByText(/Approvals 0\/1/)).toBeInTheDocument()
    expect(screen.getByText(/1 stale/)).toBeInTheDocument()
  })

  it('explains an insufficient-approvals merge rejection', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 0 },
      entries: [],
    })
    const error = new ApiError('409 Conflict', 409)
    error.detail = { insufficient_approvals: { required: 2, current: 1 } }
    vi.mocked(planBranchesApi.merge).mockRejectedValue(error)

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))
    await clickMerge()
    await confirmMerge()

    expect(
      await screen.findByText('Not enough approvals to merge: 1 of 2 required.'),
    ).toBeInTheDocument()
  })

  it('explains a merge rejection when plan entities on main changed', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: true,
      summary: { added: 1, removed: 0, changed: 0 },
      entries: [
        { entity_type: 'event', kind: 'added', name: 'checkout', parent: 'track', changes: [] },
      ],
    })
    const error = new ApiError('409 Conflict', 409)
    error.detail = { branch_behind_base: true }
    vi.mocked(planBranchesApi.merge).mockRejectedValue(error)

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))
    await clickMerge()
    await confirmMerge()

    expect(
      await screen.findByText(/main changed the same entities.*Update the branch from main/i),
    ).toBeInTheDocument()
  })

  // The decoder knew five payload shapes by key and fell through on anything
  // else. api/client.ts promotes only a STRING `detail` into ApiError.message,
  // so an object one leaves the message as the literal "409 Conflict" and parks
  // the body on `error.detail` — a reviewer got a status line and no
  // instruction. Both payloads below carry the backend's own wording in
  // `message` and neither had an arm: `merge_constraint_violation` is new
  // and `incomplete_base_snapshot` had been in the same hole all
  // along.
  const undecoded409s: Array<[string, Record<string, unknown>, RegExp]> = [
    [
      'a constraint the merge would break on main',
      {
        merge_constraint_violation: true,
        message:
          'Merging this branch would break a uniqueness rule on main — most often ' +
          'two rows ending up with the same name or the same scan identity. Rename ' +
          'the clashing entity on the branch and merge again.',
      },
      /uniqueness rule on main/,
    ],
    [
      'a branch predating the complete merge baseline',
      {
        incomplete_base_snapshot: true,
        message:
          'This branch predates the complete merge baseline. Recreate it from ' +
          'current main before merging.',
      },
      /predates the complete merge baseline/,
    ],
  ]

  it.each(undecoded409s)('reads the merge gate its own words for %s', async (_label, detail, expected) => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 0 },
      entries: [],
    })
    const error = new ApiError('409 Conflict', 409)
    error.detail = detail
    vi.mocked(planBranchesApi.merge).mockRejectedValue(error)

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))
    await clickMerge()
    await confirmMerge()

    expect(await screen.findByText(expected)).toBeInTheDocument()
    // The bare status line is what the reviewer used to be left holding.
    expect(screen.queryByText('409 Conflict')).not.toBeInTheDocument()
  })

  it('keeps the hand-written wording for a shape that has one, message or not', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: true,
      summary: { added: 0, removed: 0, changed: 0 },
      entries: [],
    })
    // Pins the ORDER the generic arm has to sit in. A `message` also travels on
    // payloads the five arms above already word for this page's own vocabulary;
    // reading it first would silently replace all five with backend prose.
    const error = new ApiError('409 Conflict', 409)
    error.detail = { branch_behind_base: true, message: 'Raw backend prose.' }
    vi.mocked(planBranchesApi.merge).mockRejectedValue(error)

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))
    await clickMerge()
    await confirmMerge()

    expect(
      await screen.findByText(/main changed the same entities.*Update the branch from main/i),
    ).toBeInTheDocument()
    expect(screen.queryByText('Raw backend prose.')).not.toBeInTheDocument()
  })

  it('warns before merging when the diff removes properties from main', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 1, changed: 0 },
      entries: [
        { entity_type: 'variable', kind: 'removed', name: 'variant', parent: null, changes: [] },
      ],
    })
    vi.mocked(planBranchesApi.merge).mockResolvedValue({} as never)

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))
    await clickMerge()

    // The confirm dialog lists the doomed variable; merge waits for consent.
    expect(await screen.findByText('Merge deletes properties from main')).toBeInTheDocument()
    expect(screen.getByText(/removes 1 property from main: variant/)).toBeInTheDocument()
    expect(planBranchesApi.merge).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Merge anyway' }))
    await waitFor(() => expect(planBranchesApi.merge).toHaveBeenCalledWith('demo', 'feat-1'))
  })

  /** A branch that renames a variable produces a removal AND an addition in the
   * diff, but the merge pairs them and renames main's row in place — nothing is
   * deleted, so nothing may be warned about and the two rows are one change.
   *
   * `paired` is the backend's `renames`, and it is the ONLY thing that decides:
   * the entries below deliberately keep a matching `source_name` on both sides
   * whether or not the pair is stated, because that is what this screen used to
   * pair on for itself. It cannot: the real rule also refuses a move onto a name
   * a staying main row holds, which a base-to-branch diff cannot see.
   *
   * Typed as the shared `PlanBranchDiffSummary` and nothing else: `renames` is a
   * field of that type, declared once in `types/branches.ts` and mirrored by the
   * generated client. A local structural twin of it used to live in
   * BranchesTab.tsx and this mock was typed against that — two exported shapes
   * with one name, which is the drift this wave argues against. */
  function mockRenamedVariableDiff(options: { behindBase?: boolean; paired?: boolean } = {}) {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    const diff: PlanBranchDiffSummary = {
      behind_base: options.behindBase ?? false,
      summary: { added: 1, removed: 1, changed: 0 },
      entries: [
        {
          entity_type: 'variable',
          kind: 'removed',
          name: 'variant',
          parent: null,
          changes: [],
          before: { name: 'variant', source_name: 'payload.variant' },
        },
        {
          entity_type: 'variable',
          kind: 'added',
          name: 'experiment_variant',
          parent: null,
          changes: [],
          after: { name: 'experiment_variant', source_name: 'payload.variant' },
        },
      ],
      renames: (options.paired ?? true)
        ? [
            {
              entity_type: 'variable',
              parent: null,
              removed_name: 'variant',
              added_name: 'experiment_variant',
            },
          ]
        : [],
    }
    vi.mocked(planBranchesApi.diff).mockResolvedValue(diff)
    vi.mocked(planBranchesApi.merge).mockResolvedValue({} as never)
  }

  it('shows a rename the backend paired as one row, not a removal and an addition', async () => {
    mockRenamedVariableDiff()

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))
    await waitFor(() => expect(planBranchesApi.diff).toHaveBeenCalledWith('demo', 'feat-1'))

    // One row, named for the row that survives, pointing at the name it took.
    expect(await screen.findByText('1 change')).toBeInTheDocument()
    expect(screen.getByText('Renamed')).toBeInTheDocument()
    expect(screen.getByText('→ experiment_variant')).toBeInTheDocument()
    // The scary half and the misleading half are both gone: nothing was removed
    // from the branch and nothing was created on it.
    expect(screen.queryByText('Removed')).not.toBeInTheDocument()
    expect(screen.queryByText('Added')).not.toBeInTheDocument()
  })

  it('reverts a paired rename through its removed half, so the row is kept', async () => {
    mockRenamedVariableDiff()
    vi.mocked(planBranchesApi.revert).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 0 },
      entries: [],
    })

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))
    fireEvent.click(await screen.findByText('variant'))
    fireEvent.click(await screen.findByRole('button', { name: /Undo this rename/i }))
    fireEvent.click(await screen.findByRole('button', { name: 'Undo rename' }))

    // The REMOVED half: that is the request the backend answers by moving the
    // name back onto the row. Sending the added half would delete the row and
    // cascade the observed values away.
    await waitFor(() =>
      expect(planBranchesApi.revert).toHaveBeenCalledWith('demo', 'feat-1', {
        entity_type: 'variable',
        name: 'variant',
        parent: null,
        field: null,
        entity_id: null,
      }),
    )
  })

  it('does not warn about a property the merge will rename rather than delete', async () => {
    mockRenamedVariableDiff()

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))
    await clickMerge()

    // The plain confirm, not the deletion warning: the paired row keeps its
    // id, and with it the observed values, overrides and drift history the
    // warning is about.
    expect(await screen.findByText(/1 renamed\)\?/)).toBeInTheDocument()
    await confirmMerge()
    await waitFor(() => expect(planBranchesApi.merge).toHaveBeenCalledWith('demo', 'feat-1'))
    expect(screen.queryByText('Merge deletes properties from main')).not.toBeInTheDocument()
  })

  it('warns about a removal the backend did not pair, matching identities or not', async () => {
    mockRenamedVariableDiff({ paired: false })

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))
    await clickMerge()

    // Both entries carry `payload.variant`, which is exactly the shape this
    // screen used to pair for itself. The merge refuses the move — main already
    // holds `experiment_variant` — so `variant` really is deleted, and only the
    // backend can know that.
    expect(await screen.findByText('Merge deletes properties from main')).toBeInTheDocument()
    expect(screen.getByText(/removes 1 property from main: variant/)).toBeInTheDocument()
    expect(planBranchesApi.merge).not.toHaveBeenCalled()
  })

  it('trusts a stated pairing even while the branch is behind its base', async () => {
    mockRenamedVariableDiff({ behindBase: true })

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))
    await clickMerge()

    // Being behind used to force the warning on every removal, because the
    // pairing was inferred from a diff that cannot see main. The backend's
    // pairing already read main, so there is nothing left to be cautious about.
    await confirmMerge()
    await waitFor(() => expect(planBranchesApi.merge).toHaveBeenCalledWith('demo', 'feat-1'))
    expect(screen.queryByText('Merge deletes properties from main')).not.toBeInTheDocument()
  })

  it('counts a paired rename once in the strip, the ahead badge and the subtitle', async () => {
    // The three numbers describe one diff, so they have to agree. They did not:
    // the list dropped the paired addition and re-counted its own subtitle,
    // while the strip and the ahead badge went on reading the backend's raw
    // per-kind tally. One branch therefore said "↑2", "+1 added · −1 removed"
    // and "1 change" over a single Renamed row, all on one screen — and the red
    // "−1 removed" is exactly the false deletion signal the Renamed row exists
    // to remove.
    mockRenamedVariableDiff()

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))
    await waitFor(() => expect(planBranchesApi.diff).toHaveBeenCalledWith('demo', 'feat-1'))

    // The panel subtitle over the single Renamed row.
    expect(await screen.findByText('1 change')).toBeInTheDocument()
    // The list row's ahead badge: one change ahead, not two.
    expect(screen.getByText('↑1')).toBeInTheDocument()
    // The header strip: the rename is subtracted from both halves it was split
    // into, and named, so the drop is explained rather than silent.
    expect(screen.getByText('+0')).toBeInTheDocument()
    expect(screen.getByText('~0')).toBeInTheDocument()
    expect(screen.getByText('−0')).toBeInTheDocument()
    expect(screen.getByText('→1')).toBeInTheDocument()
    expect(screen.getByText('renamed')).toBeInTheDocument()
    // The counts that made the screen contradict itself.
    expect(screen.queryByText('+1')).not.toBeInTheDocument()
    expect(screen.queryByText('−1')).not.toBeInTheDocument()
    expect(screen.queryByText('↑2')).not.toBeInTheDocument()
  })

  it('leaves the renamed chip off a diff that renamed nothing', async () => {
    // The three counts are the strip every branch shows; a permanent "→0
    // renamed" would be noise on all of them. The chip is the explanation for a
    // drop, so it appears only when there is a drop.
    mockRenamedVariableDiff({ paired: false })

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))
    await waitFor(() => expect(planBranchesApi.diff).toHaveBeenCalledWith('demo', 'feat-1'))

    // Nothing paired: the raw counts stand, and so does the "2 ahead".
    expect(await screen.findByText('+1')).toBeInTheDocument()
    expect(screen.getByText('−1')).toBeInTheDocument()
    expect(screen.getByText('↑2')).toBeInTheDocument()
    expect(screen.queryByText('renamed')).not.toBeInTheDocument()
  })

  it('offers to undo the rename the revert will really perform, not a restore', async () => {
    // The merge refuses this pairing because main independently grew its own
    // `experiment_variant` — that is what `renames: []` says here. The REVERT
    // asks a narrower, main-free question (`_row_renamed_from`), finds the
    // branch row still carrying `payload.variant`, and moves the name back onto
    // it: the addition the reviewer was looking at disappears and nothing is
    // restored. The dialog used to read the merge's "no" and promise a restore.
    mockRenamedVariableDiff({ paired: false })
    vi.mocked(planBranchesApi.revert).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 0 },
      entries: [],
    })

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))
    fireEvent.click(await screen.findByText('variant'))
    // The row still reads the merge's answer — it is a Removed row, because
    // that is what the merge will do with it — so the reviewer arrives at the
    // dialog expecting a restore. The dialog is where that is corrected.
    fireEvent.click(await screen.findByRole('button', { name: /Restore on this branch/i }))

    expect(
      await screen.findByText(/Undo the rename of variant to experiment_variant/),
    ).toBeInTheDocument()
    // The title and the label both change with it. (The title is asserted by
    // its absence: "Undo rename" is also the confirm label, so matching that
    // string by text would find two elements.)
    expect(screen.getByRole('button', { name: 'Undo rename' })).toBeInTheDocument()
    expect(screen.queryByText('Revert change')).not.toBeInTheDocument()
    // The promise the button cannot keep.
    expect(screen.queryByText(/photos are not restored/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Restore' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Undo rename' }))
    await waitFor(() =>
      expect(planBranchesApi.revert).toHaveBeenCalledWith('demo', 'feat-1', {
        entity_type: 'variable',
        name: 'variant',
        parent: null,
        field: null,
        entity_id: null,
      }),
    )
  })

  it('keeps the restore wording for a removal no branch row answers to', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 1, removed: 1, changed: 0 },
      entries: [
        {
          entity_type: 'variable',
          kind: 'removed',
          name: 'variant',
          parent: null,
          changes: [],
          before: { name: 'variant', source_name: 'payload.variant' },
        },
        // A genuine addition: no `source_name` of its own, so it answers to
        // nothing and `_row_renamed_from` would find no row to move.
        {
          entity_type: 'variable',
          kind: 'added',
          name: 'cohort',
          parent: null,
          changes: [],
          after: { name: 'cohort', source_name: null },
        },
      ],
    })

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))
    fireEvent.click(await screen.findByText('variant'))
    fireEvent.click(await screen.findByRole('button', { name: /Restore on this branch/i }))

    // Nothing on the branch carries the removed row's identity, so this really
    // is a rebuild — including the photo loss it cannot undo.
    expect(await screen.findByText('Revert change')).toBeInTheDocument()
    expect(screen.getByText(/its photos are not restored/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Restore' })).toBeInTheDocument()
    expect(screen.queryByText(/Undo the rename/)).not.toBeInTheDocument()
  })

  it('offers no revert when two branch rows claim the identity, and says why', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    // The fixture models a diff payload, not live rows. Two live events with one
    // `source_name` under one type are as impossible as two such variables —
    // `uq_event_scan_identity` beside `uq_variable_project_source_name` — so
    // this shape now reaches the dialog only through data the key never
    // checked, and the dialog must still refuse it rather than guess:
    // `_row_renamed_from` raises a 409 rather than rename a sibling the
    // reviewer never looked at, so neither "Restore" nor "Undo rename" is true.
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 2, removed: 1, changed: 0 },
      entries: [
        {
          entity_type: 'event',
          kind: 'removed',
          name: 'promo_applied',
          parent: 'track',
          changes: [],
          before: { name: 'promo_applied', source_name: 'promo' },
        },
        {
          entity_type: 'event',
          kind: 'added',
          name: 'promo_code_applied',
          parent: 'track',
          changes: [],
          after: { name: 'promo_code_applied', source_name: 'promo' },
        },
        {
          entity_type: 'event',
          kind: 'added',
          name: 'promo_banner_applied',
          parent: 'track',
          changes: [],
          after: { name: 'promo_banner_applied', source_name: 'promo' },
        },
      ],
    })

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))
    fireEvent.click(await screen.findByText('promo_applied'))

    // No revert to click, and so no dialog offering "Try anyway" for a request
    // it has just said will be refused: the row says why, and names
    // the rows to fix by hand.
    const note = await screen.findByRole('note')
    expect(note).toHaveTextContent(/Can’t revert: 2 rows on this branch carry promo_applied’s scan identity/)
    expect(within(note).getByText('promo_banner_applied')).toBeInTheDocument()
    expect(within(note).getByText('promo_code_applied')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Restore on this branch/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Try anyway' })).not.toBeInTheDocument()
    expect(planBranchesApi.revert).not.toHaveBeenCalled()
  })

  it('scopes the rename check to the parent, like the endpoint scopes its query', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({ entities: [], unresolved_count: 0 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    // Two events under DIFFERENT event types may share a `source_name`; only one
    // under THIS type can be the row that moved, which is why the endpoint joins
    // EventType and matches `data.parent`. Ignoring the scope here would call a
    // deletion under `track` a rename into a row under `screen`.
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 1, removed: 1, changed: 0 },
      entries: [
        {
          entity_type: 'event',
          kind: 'removed',
          name: 'promo_applied',
          parent: 'track',
          changes: [],
          before: { name: 'promo_applied', source_name: 'promo' },
        },
        {
          entity_type: 'event',
          kind: 'added',
          name: 'promo_viewed',
          parent: 'screen',
          changes: [],
          after: { name: 'promo_viewed', source_name: 'promo' },
        },
      ],
    })

    renderTab()

    fireEvent.click(await screen.findByText('checkout-v2'))
    fireEvent.click(await screen.findByText('promo_applied'))
    fireEvent.click(await screen.findByRole('button', { name: /Restore on this branch/i }))

    expect(await screen.findByText('Revert change')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Restore' })).toBeInTheDocument()
    expect(screen.queryByText(/Undo the rename/)).not.toBeInTheDocument()
  })

  it('links a merged branch to the tracker ticket the merge opened', async () => {
    mockBranchDetailQueries([MAIN, MERGED])
    vi.mocked(planBranchesApi.listImplementationTickets).mockResolvedValue([makeTicket()])

    renderTab('feat-merged')

    // The href is the whole point: without it the merge leaves no way to reach
    // the Jira issue it created.
    const link = await screen.findByRole('link', { name: /ENG-42/ })
    expect(link).toHaveAttribute('href', 'https://example.atlassian.net/browse/ENG-42')
    expect(link).toHaveAttribute('target', '_blank')
    expect(screen.getByText('Implementation ticket')).toBeInTheDocument()
    expect(screen.getByText('Implement checkout-v3')).toBeInTheDocument()
    expect(screen.getByText('Open')).toBeInTheDocument()
  })

  it('marks the ticket done once the tracker closed it', async () => {
    mockBranchDetailQueries([MAIN, MERGED])
    vi.mocked(planBranchesApi.listImplementationTickets).mockResolvedValue([
      makeTicket({ status: 'closed', closed_at: '2026-02-09T00:00:00Z' }),
    ])

    renderTab('feat-merged')

    expect(await screen.findByText('Done')).toBeInTheDocument()
    expect(screen.queryByText('Open')).not.toBeInTheDocument()
  })

  it('omits the ticket section entirely when the merge opened none', async () => {
    mockBranchDetailQueries([MAIN, MERGED])
    vi.mocked(planBranchesApi.listImplementationTickets).mockResolvedValue([])

    renderTab('feat-merged')

    await waitFor(() =>
      expect(planBranchesApi.listImplementationTickets).toHaveBeenCalledWith('demo', 'feat-merged'),
    )
    // No empty panel: a branch merged with no tracker configured is the norm.
    expect(screen.queryByText('Implementation ticket')).not.toBeInTheDocument()
    expect(screen.queryByText('Implementation tickets')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /ENG-42/ })).not.toBeInTheDocument()
  })

  it('does not ask for tickets before the branch has merged', async () => {
    mockBranchDetailQueries([MAIN, FEATURE])

    renderTab('feat-1')

    // A ticket only exists after a merge, so an open branch's list is provably
    // empty — spending a request on it would be pure waste.
    await waitFor(() => expect(planBranchesApi.diff).toHaveBeenCalledWith('demo', 'feat-1'))
    expect(planBranchesApi.listImplementationTickets).not.toHaveBeenCalled()
  })
})

describe('BranchesTab housekeeping rows', () => {
  it('folds machine removals into one uncounted line, opened on request', async () => {
    mockBranchDetailQueries([MAIN, FEATURE])
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: true,
      summary: { added: 1, removed: 0, changed: 0, housekeeping: 3 },
      entries: [
        {
          entity_type: 'event',
          kind: 'added',
          name: 'weather_alert:show:widget',
          parent: 'se',
          changes: [],
          field_changes: [],
          before: null,
          after: { name: 'weather_alert:show:widget' },
        },
        ...['property.adana', 'property.79602'].map((name) => ({
          entity_type: 'variable' as const,
          kind: 'removed' as const,
          name,
          parent: null,
          changes: [],
          field_changes: [],
          before: { name, description: 'Auto-detected property from data source scan' },
          after: null,
          housekeeping: 'unused scan variable retired',
        })),
        {
          entity_type: 'variable',
          kind: 'removed',
          name: 'property.city',
          parent: null,
          changes: [],
          field_changes: [],
          before: { name: 'property.city' },
          after: null,
          housekeeping: 'already removed on main',
        },
      ],
    })

    renderTab('feat-1')

    expect(await screen.findByText('weather_alert:show:widget')).toBeInTheDocument()
    // The reviewer's counts: one addition, no removals.
    expect(screen.getByText('+1')).toBeInTheDocument()
    expect(screen.queryByText('−3')).not.toBeInTheDocument()
    expect(screen.getByText('1 change')).toBeInTheDocument()

    const fold = screen.getByRole('button', {
      name: /2 unused scan properties retired · 1 removal already made on main/,
    })
    expect(fold).toHaveAttribute('aria-expanded', 'false')
    // No idref to a list that is not rendered.
    expect(fold).not.toHaveAttribute('aria-controls')
    expect(screen.queryByText('property.adana')).not.toBeInTheDocument()

    fireEvent.click(fold)
    expect(fold).toHaveAttribute('aria-controls')
    expect(screen.getByText('property.adana')).toBeInTheDocument()
    expect(screen.getByText('property.city')).toBeInTheDocument()
    expect(screen.getAllByText(/unused scan property retired/)).toHaveLength(2)

    // Nor does the merge confirmation list them: the dialog protects documented
    // values, overrides and drift history, none of which these rows have.
    vi.mocked(planBranchesApi.merge).mockResolvedValue({} as never)
    await clickMerge()
    await confirmMerge()
    await waitFor(() => expect(planBranchesApi.merge).toHaveBeenCalledWith('demo', 'feat-1'))
    expect(screen.queryByText('Merge deletes properties from main')).not.toBeInTheDocument()
  })
})

describe('BranchesTab ticket link', () => {
  const PROJ = makeBranch({ id: 'feat-proj', name: 'PROJ-4770', kind: 'working', status: 'draft' })

  it('links a branch named after a ticket through the meta field that links to it', async () => {
    mockBranchDetailQueries([MAIN, PROJ])
    vi.mocked(metaFieldsApi.list).mockResolvedValue([
      {
        id: 'mf-jira',
        project_id: 'p-1',
        name: 'jira',
        display_name: 'Jira',
        field_type: 'string',
        is_required: false,
        enum_options: null,
        default_value: null,
        link_template: 'https://jira.example/browse/${value}',
        order: 0,
        sensitivity: 'none',
      },
    ])

    renderTab('feat-proj')

    const link = await screen.findByRole('link', { name: /PROJ-4770/ })
    expect(link).toHaveAttribute('href', 'https://jira.example/browse/PROJ-4770')
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('shows nothing when no meta field can link a key', async () => {
    mockBranchDetailQueries([MAIN, PROJ])
    renderTab('feat-proj')

    await waitFor(() => expect(planBranchesApi.diff).toHaveBeenCalledWith('demo', 'feat-proj'))
    await waitFor(() => expect(metaFieldsApi.list).toHaveBeenCalledWith('demo'))
    expect(screen.queryByRole('link', { name: /PROJ-4770/ })).not.toBeInTheDocument()
  })
})

describe('BranchesTab accessibility', () => {
  it('has no axe violations on the branch list and an open branch', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    renderTab('feat-1')
    await screen.findAllByText('checkout-v2')
    await expectNoAxeViolations(document.body)
  })
})

describe('BranchesTab review flows (frontend review batch 14)', () => {
  const CONFLICTED: PlanBranchConflicts = {
    entities: [
      {
        entity_type: 'event_type',
        name: 'checkout',
        fields: [
          {
            field: 'description',
            base: 'old',
            ours: 'main edit',
            theirs: 'branch edit',
            choice: null,
            dependents: 0,
          },
        ],
      },
    ],
    unresolved_count: 1,
  }

  it('reads the row badges from one counted list, not one diff per branch', async () => {
    const other = {
      ...makeBranch({ id: 'feat-o', name: 'other-work', kind: 'working', status: 'draft' }),
      ahead: 4,
      behind_base: true,
    }
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE, other], total: 3 })

    renderTab('feat-1')

    await waitFor(() =>
      expect(planBranchesApi.list).toHaveBeenCalledWith('demo', { include_diff_counts: true }),
    )
    expect(await screen.findByText('↑4')).toBeInTheDocument()
    expect(screen.getByText('4 changes ahead of main')).toBeInTheDocument()
    await waitFor(() => expect(planBranchesApi.diff).toHaveBeenCalledWith('demo', 'feat-1'))
    expect(planBranchesApi.diff).not.toHaveBeenCalledWith('demo', 'feat-o')
  })

  it('marks the selected branch row as the current one', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })

    renderTab('feat-1')

    const row = await screen.findByRole('button', { name: /checkout-v2/ })
    expect(row).toHaveAttribute('aria-current', 'true')
    expect(screen.getByRole('button', { name: /^main/ })).not.toHaveAttribute('aria-current')
  })

  it("refreshes main's plan caches and the conflicts after a merge", async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.merge).mockResolvedValue({} as never)
    const invalidate = vi.spyOn(QueryClient.prototype, 'invalidateQueries')

    renderTab('feat-1')

    await clickMerge()
    await confirmMerge()
    await waitFor(() => expect(planBranchesApi.merge).toHaveBeenCalledWith('demo', 'feat-1'))
    await waitFor(() => {
      const keys = invalidate.mock.calls.map(([filters]) => filters?.queryKey)
      for (const key of [
        ['eventTypes', 'demo'],
        ['variables', 'demo'],
        ['events', 'demo'],
        ['event', 'demo'],
        ['eventTags', 'demo'],
        ['eventHistory', 'demo'],
        ['metaFields', 'demo'],
        ['relations', 'demo'],
        ['planRevisions', 'demo'],
        ['planBranchConflicts', 'demo', 'feat-1'],
        ['planBranchCounts', 'demo'],
      ]) {
        expect(keys).toContainEqual(key)
      }
    })
  })

  it('does not merge when the reviewer cancels the confirm', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })

    renderTab('feat-1')

    await clickMerge()
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(planBranchesApi.merge).not.toHaveBeenCalled()
  })

  it('asks before closing a branch', async () => {
    const draft = makeBranch({ id: 'feat-d', name: 'draft-work', kind: 'working', status: 'draft' })
    mockBranchDetailQueries([MAIN, draft])
    vi.mocked(planBranchesApi.transition).mockResolvedValue({} as never)

    renderTab('feat-d')

    fireEvent.click(await screen.findByRole('button', { name: 'Close' }))
    expect(planBranchesApi.transition).not.toHaveBeenCalled()
    fireEvent.click(await screen.findByRole('button', { name: 'Close branch' }))
    await waitFor(() =>
      expect(planBranchesApi.transition).toHaveBeenCalledWith('demo', 'feat-d', 'close'),
    )
  })

  it('refetches the conflicts a merge was refused over, so "below" has them', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    // Empty when the panel first loaded; main has since gained a conflicting edit.
    vi.mocked(planBranchesApi.getConflicts)
      .mockResolvedValueOnce({ entities: [], unresolved_count: 0 })
      .mockResolvedValue(CONFLICTED)
    const error = new ApiError('409 Conflict', 409)
    error.detail = { unresolved_field_conflicts: [{ entity: 'checkout', field: 'description' }] }
    vi.mocked(planBranchesApi.merge).mockRejectedValue(error)

    renderTab('feat-1')

    await waitFor(() => expect(planBranchesApi.getConflicts).toHaveBeenCalledTimes(1))
    expect(screen.queryByText('Conflicts')).not.toBeInTheDocument()

    await clickMerge()
    await confirmMerge()

    expect(
      await screen.findByText('Merge blocked: resolve the field conflicts below first.'),
    ).toBeInTheDocument()
    expect(await screen.findByText('Conflicts')).toBeInTheDocument()
    expect(screen.getByText('1 unresolved')).toBeInTheDocument()
  })

  it('names the sides of a conflict and says which one is chosen', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({
      ...CONFLICTED,
      entities: [
        {
          ...at(CONFLICTED.entities, 0),
          fields: [{ ...at(at(CONFLICTED.entities, 0).fields, 0), choice: 'theirs' }],
        },
      ],
      unresolved_count: 0,
    })
    vi.mocked(planBranchesApi.saveResolution).mockRejectedValue(new ApiError('Forbidden', 403))

    renderTab('feat-1')

    expect(await screen.findByText('This branch:')).toBeInTheDocument()
    expect(screen.getByText('Main now:')).toBeInTheDocument()
    expect(screen.getByText('Was (when the branch opened):')).toBeInTheDocument()
    expect(screen.getByText("Resolved: this branch's value")).toBeInTheDocument()
    const branchSide = screen.getByRole('button', { name: 'Keep this branch' })
    const mainSide = screen.getByRole('button', { name: 'Take main' })
    expect(branchSide).toHaveAttribute('aria-pressed', 'true')
    expect(mainSide).toHaveAttribute('aria-pressed', 'false')
    expect(screen.queryByText(/\bours\b|\btheirs\b/)).not.toBeInTheDocument()

    fireEvent.click(mainSide)

    // The entity's own type, not a hard-coded 'event_type'.
    await waitFor(() =>
      expect(planBranchesApi.saveResolution).toHaveBeenCalledWith('demo', 'feat-1', {
        entity_type: 'event_type',
        entity_name: 'checkout',
        field_name: 'description',
        choice: 'ours',
      }),
    )
    // A failed save used to leave the buttons as they were and say nothing.
    expect(await screen.findByText('Could not save the choice for checkout: Forbidden')).toBeInTheDocument()
  })

  it('does not compute conflicts for a landed branch', async () => {
    mockBranchDetailQueries([MAIN, MERGED])

    renderTab('feat-merged')

    await waitFor(() => expect(planBranchesApi.diff).toHaveBeenCalledWith('demo', 'feat-merged'))
    expect(planBranchesApi.getConflicts).not.toHaveBeenCalled()
  })

  it('holds the delete button while it runs and shows a failed delete', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    let rejectDelete: (error: unknown) => void = () => {}
    vi.mocked(planBranchesApi.delete).mockImplementation(
      () =>
        new Promise<never>((_resolve, reject) => {
          rejectDelete = reject
        }),
    )

    renderTab('feat-1')

    fireEvent.click(await screen.findByRole('button', { name: 'Delete branch' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))

    await waitFor(() => expect(screen.getByRole('button', { name: 'Delete branch' })).toBeDisabled())
    rejectDelete(new ApiError('Branch is protected', 409))

    expect(
      await screen.findByText('Could not delete the branch: Branch is protected'),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete branch' })).toBeEnabled()
    expect(planBranchesApi.delete).toHaveBeenCalledTimes(1)
  })

  it('leaves a deleted branch for main, and says so', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.delete).mockResolvedValue(undefined as never)
    const success = vi.spyOn(toast, 'success')

    renderTab('feat-1')

    fireEvent.click(await screen.findByRole('button', { name: 'Delete branch' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))

    expect(await screen.findByText(/every change merges here/i)).toBeInTheDocument()
    expect(success).toHaveBeenCalledWith(expect.stringContaining('checkout-v2'))
  })

  it('offers no delete on a merged branch', async () => {
    mockBranchDetailQueries([MAIN, MERGED])

    renderTab('feat-merged')

    await waitFor(() => expect(planBranchesApi.diff).toHaveBeenCalledWith('demo', 'feat-merged'))
    // The detail header's status chip and the list row's.
    expect((await screen.findAllByText('Merged')).length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: 'Delete branch' })).not.toBeInTheDocument()
  })

  it('says a branch in the URL is gone instead of showing main', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })

    renderTab('feat-gone')

    expect(await screen.findByText('Branch not found')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to main' })).toHaveAttribute(
      'href',
      '/p/demo/branches',
    )
    expect(screen.queryByText(/every change merges here/i)).not.toBeInTheDocument()
  })

  it('waits for the tracker ticket the merge worker is still creating', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.merge).mockResolvedValue({} as never)
    vi.mocked(trackerConfigApi.get).mockResolvedValue(makeTrackerConfig({ enabled: true }))
    // The worker has not written the row when the panel first asks.
    vi.mocked(planBranchesApi.listImplementationTickets)
      .mockResolvedValueOnce([])
      .mockResolvedValue([makeTicket({ branch_id: 'feat-1' })])

    renderTab('feat-1')

    await clickMerge()
    await confirmMerge()

    expect(
      await screen.findByText('Creating the tracker ticket for this merge…'),
    ).toBeInTheDocument()
    const link = await screen.findByRole('link', { name: /ENG-42/ }, { timeout: 6000 })
    expect(link).toHaveAttribute('href', 'https://example.atlassian.net/browse/ENG-42')
  })

  it('does not offer the author a live Approve when self-approval is blocked', async () => {
    const own = makeBranch({
      id: 'feat-own',
      name: 'my-work',
      kind: 'working',
      status: 'ready_for_review',
      created_by: 'owner-1',
    })
    mockBranchDetailQueries([MAIN, own])
    vi.mocked(branchSettingsApi.get).mockResolvedValue(makeSettings({ block_self_approval: true }))

    renderTab('feat-own')

    expect(await screen.findByText('No changes in this branch.')).toBeInTheDocument()
    const approve = await screen.findByRole('button', { name: 'Approve' })
    // The reason is written next to the button, where a `title` on a disabled
    // button never showed (#237), and the button points at it.
    const reason = await screen.findByText("Authors can't approve their own branch (merge policy).")
    await waitFor(() => expect(approve).toBeDisabled())
    expect(approve.getAttribute('aria-describedby')).toBe(reason.parentElement?.id)
  })

  it('keeps expanded rows open when a row above them is reverted', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    const beta = {
      entity_type: 'event' as const,
      kind: 'changed' as const,
      name: 'beta_event',
      parent: 'track',
      changes: ['owner changed'],
      field_changes: [{ field: 'owner', before: 'a', after: 'b', items: [] }],
    }
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 2 },
      entries: [
        {
          entity_type: 'event',
          kind: 'changed',
          name: 'alpha_event',
          parent: 'track',
          changes: ['title changed'],
          field_changes: [{ field: 'title', before: 'x', after: 'y', items: [] }],
        },
        beta,
      ],
    })
    vi.mocked(planBranchesApi.revert).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 1 },
      entries: [beta],
    })

    renderTab('feat-1')

    fireEvent.click(await screen.findByRole('button', { name: /beta_event/ }))
    fireEvent.click(screen.getByRole('button', { name: /alpha_event/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Revert title' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Revert' }))

    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /alpha_event/ })).not.toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: /beta_event/ })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
    // The revert answered with the resulting diff; no second diff request.
    expect(planBranchesApi.diff).toHaveBeenCalledTimes(1)
  })

  it('labels the before and after of a field change for assistive tech', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 1 },
      entries: [
        {
          entity_type: 'event',
          kind: 'changed',
          name: 'purchase',
          parent: 'track',
          changes: ['currency changed'],
          field_changes: [{ field: 'currency', before: 'USD', after: 'EUR', items: [] }],
        },
      ],
    })

    renderTab('feat-1')

    fireEvent.click(await screen.findByRole('button', { name: /purchase/ }))
    expect(screen.getByText('before:')).toBeInTheDocument()
    expect(screen.getByText('after:')).toBeInTheDocument()
    expect(screen.getByText('USD')).toBeInTheDocument()
    expect(screen.getByText('EUR')).toBeInTheDocument()
  })

  it('shows what the branch is for, who approved it and who reviews it', async () => {
    const described = { ...FEATURE, description: 'Adds the checkout v2 events.' }
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, described], total: 2 })
    vi.mocked(planBranchesApi.get).mockResolvedValue({
      ...described,
      reviewers: [{ id: 'r-1', user_id: 'u-priya', created_at: '2026-01-01T00:00:00Z' }],
      approvals: [{ user_id: 'u-maya', approved_at: '2026-01-02T00:00:00Z', stale: true }],
    })
    vi.mocked(planBranchesApi.addReviewer).mockResolvedValue({
      id: 'r-2',
      user_id: 'u-maya',
      created_at: '2026-01-03T00:00:00Z',
    })

    renderTab('feat-1')

    expect(await screen.findByText('Adds the checkout v2 events.')).toBeInTheDocument()
    expect(await screen.findByText(/Approved by/)).toHaveTextContent('Approved by Maya R. (stale)')
    expect(screen.getByText('Priya S.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove reviewer Priya S.' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Add reviewer' }))
    fireEvent.change(screen.getByLabelText('Reviewer to add'), { target: { value: 'u-maya' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    await waitFor(() =>
      expect(planBranchesApi.addReviewer).toHaveBeenCalledWith('demo', 'feat-1', 'u-maya'),
    )
  })

  it('offers only project members as reviewers', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.get).mockResolvedValue({ ...FEATURE, reviewers: [], approvals: [] })
    // Priya is on the instance roster but not a member of this project.
    vi.mocked(projectMembersApi.list).mockResolvedValue([
      {
        user_id: 'u-maya',
        name: 'Maya R.',
        email: 'maya@example.com',
        role: 'editor',
        added_at: '2026-01-01T00:00:00Z',
      },
    ])

    renderTab('feat-1')

    fireEvent.click(await screen.findByRole('button', { name: 'Add reviewer' }))
    const picker = screen.getByLabelText('Reviewer to add')
    expect(within(picker).getByRole('option', { name: 'Maya R.' })).toBeInTheDocument()
    expect(within(picker).queryByRole('option', { name: 'Priya S.' })).toBeNull()
    expect(projectMembersApi.list).toHaveBeenCalledWith('demo', expect.anything())
  })

  it('also offers instance owners, who hold no member row, once each', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.get).mockResolvedValue({ ...FEATURE, reviewers: [], approvals: [] })
    vi.mocked(usersApi.list).mockResolvedValue([
      ...USERS,
      makeUser({ id: 'u-boss', name: 'Boss K.', email: 'boss@example.com', role: 'owner' }),
      makeUser({ id: 'u-maya', name: 'Maya R.', email: 'maya@example.com', role: 'owner' }),
    ])
    vi.mocked(projectMembersApi.list).mockResolvedValue([
      {
        user_id: 'u-maya',
        name: 'Maya R.',
        email: 'maya@example.com',
        role: 'editor',
        added_at: '2026-01-01T00:00:00Z',
      },
    ])

    renderTab('feat-1')

    fireEvent.click(await screen.findByRole('button', { name: 'Add reviewer' }))
    const picker = screen.getByLabelText('Reviewer to add')
    expect(await within(picker).findByRole('option', { name: 'Boss K.' })).toBeInTheDocument()
    expect(within(picker).getAllByRole('option', { name: 'Maya R.' })).toHaveLength(1)
    expect(within(picker).queryByRole('option', { name: 'Priya S.' })).toBeNull()
  })

  it('shows a failed policy load with a retry, not "Loading policy…" forever', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN], total: 1 })
    vi.mocked(branchSettingsApi.get)
      .mockRejectedValueOnce(new Error('Network down'))
      .mockResolvedValue(makeSettings({}))

    renderTab()

    fireEvent.click(await screen.findByRole('button', { name: /Merge policy/i }))
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText('Could not load the merge policy')).toBeInTheDocument()
    expect(within(dialog).getByText('Network down')).toBeInTheDocument()

    fireEvent.click(within(dialog).getByRole('button', { name: /Try again/ }))
    expect(await within(dialog).findByLabelText('Required approvals')).toBeInTheDocument()
  })

  it('refuses a required-approvals value outside 0-100', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN], total: 1 })

    renderTab()

    fireEvent.click(await screen.findByRole('button', { name: /Merge policy/i }))
    const dialog = await screen.findByRole('dialog')
    const input = await within(dialog).findByLabelText('Required approvals')
    await waitFor(() => expect(input).toHaveValue(1))

    fireEvent.change(input, { target: { value: '150' } })

    expect(within(dialog).getByText('Enter a whole number from 0 to 100.')).toBeInTheDocument()
    expect(input).toHaveAttribute('aria-invalid', 'true')
    const save = within(dialog).getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()
    fireEvent.submit(save.closest('form')!)
    expect(branchSettingsApi.update).not.toHaveBeenCalled()
  })

  it("refreshes the branch's own editor caches after a revert", async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 1 },
      entries: [
        {
          entity_type: 'event',
          kind: 'changed',
          name: 'purchase',
          parent: 'track',
          entity_id: 'ev-1',
          changes: ['title changed'],
          field_changes: [{ field: 'title', before: 'x', after: 'y', items: [] }],
        },
      ],
    })
    vi.mocked(planBranchesApi.revert).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 0 },
      entries: [],
    })
    const invalidate = vi.spyOn(QueryClient.prototype, 'invalidateQueries')

    renderTab('feat-1')

    fireEvent.click(await screen.findByRole('button', { name: /purchase/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Revert title' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Revert' }))

    // The event editor fills from ['event', slug, branchId, eventId]; a stale
    // copy there would save the reverted values straight back.
    await waitFor(() => {
      const keys = invalidate.mock.calls.map(([filters]) => filters?.queryKey)
      for (const key of [
        ['event', 'demo', 'feat-1'],
        ['eventTags', 'demo', 'feat-1'],
        ['eventHistory', 'demo', 'feat-1'],
        ['events', 'demo', 'feat-1'],
        ['planBranchCounts', 'demo'],
      ]) {
        expect(keys).toContainEqual(key)
      }
    })
  })

  it('does not rebuild the counted list for a status change', async () => {
    const ready = makeBranch({
      id: 'feat-r',
      name: 'ready-one',
      kind: 'working',
      status: 'ready_for_review',
      created_by: 'u-maya',
    })
    mockBranchDetailQueries([MAIN, ready])
    vi.mocked(planBranchesApi.transition).mockResolvedValue({} as never)
    const countedCalls = () =>
      vi
        .mocked(planBranchesApi.list)
        .mock.calls.filter(([, options]) => options?.include_diff_counts === true).length

    renderTab('feat-r')

    await waitFor(() => expect(countedCalls()).toBe(1))
    const approve = await screen.findByRole('button', { name: 'Approve' })
    await waitFor(() => expect(approve).toBeEnabled())
    fireEvent.click(approve)
    await waitFor(() =>
      expect(planBranchesApi.transition).toHaveBeenCalledWith('demo', 'feat-r', 'approve'),
    )
    // The plain list refetches for the new status; the per-branch snapshot
    // sweep behind the badges does not, because an approval moves no count.
    await waitFor(() =>
      expect(
        vi.mocked(planBranchesApi.list).mock.calls.filter(([, options]) => !options).length,
      ).toBeGreaterThan(1),
    )
    expect(countedCalls()).toBe(1)
  })

  it('stops making Approve the primary once your own approval stands', async () => {
    const ready = makeBranch({
      id: 'feat-r',
      name: 'ready-one',
      kind: 'working',
      status: 'ready_for_review',
      created_by: 'u-maya',
    })
    mockBranchDetailQueries([MAIN, ready])
    vi.mocked(branchSettingsApi.get).mockResolvedValue(makeSettings({ min_approvals: 2 }))
    vi.mocked(planBranchesApi.get).mockResolvedValue({
      ...ready,
      reviewers: [],
      approvals: [{ user_id: 'owner-1', approved_at: '2026-01-02T00:00:00Z', stale: false }],
    })

    renderTab('feat-r')

    await screen.findByText('Approvals 1/2')
    const approve = await screen.findByRole('button', { name: 'Approve' })
    // Filled means "your next step"; with your approval in, it is not.
    expect(approve).toHaveClass('border-input')
    expect(approve).not.toHaveClass('bg-accent-solid')
  })

  it('switches the shell back to main when the active branch is deleted', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.delete).mockResolvedValue(undefined as never)
    localStorage.setItem('tripl-branch:demo', FEATURE.id)

    function ActiveBranch() {
      return <output aria-label="active branch">{useActiveBranchId() ?? 'main'}</output>
    }
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={authAs('owner')}>
          <MemoryRouter initialEntries={[`/p/demo/branches/${FEATURE.id}`]}>
            <BranchProvider slug="demo">
              <ActiveBranch />
              <Routes>
                <Route path="/p/:slug/branches" element={<BranchesTabRoute />} />
                <Route path="/p/:slug/branches/:branchId" element={<BranchesTabRoute />} />
              </Routes>
            </BranchProvider>
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )
    const active = screen.getByRole('status', { name: 'active branch' })
    expect(active).toHaveTextContent(FEATURE.id)

    fireEvent.click(await screen.findByRole('button', { name: 'Delete branch' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))

    await waitFor(() => expect(active).toHaveTextContent('main'))
    expect(localStorage.getItem('tripl-branch:demo')).toBeNull()
  })

  it('reaches the settings dialogs through the narrow-screen menu', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN], total: 1 })

    renderTab()

    fireEvent.keyDown(await screen.findByRole('button', { name: 'Branch settings' }), {
      key: 'Enter',
    })
    fireEvent.click(await screen.findByRole('menuitem', { name: /Merge policy/ }))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Merge policy')).toBeInTheDocument()
  })

  it('marks the words a long text change touched, on each side', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: false,
      summary: { added: 0, removed: 0, changed: 1 },
      entries: [
        {
          entity_type: 'event',
          kind: 'changed',
          name: 'purchase',
          parent: 'track',
          changes: ['description changed'],
          field_changes: [
            {
              field: 'description',
              before: 'Fired when the user completes a purchase on the web checkout',
              after: 'Fired when the user completes a purchase on the mobile checkout',
              items: [],
            },
          ],
        },
      ],
    })

    renderTab('feat-1')

    fireEvent.click(await screen.findByRole('button', { name: /purchase/ }))
    expect(screen.getByText('web').closest('del')).not.toBeNull()
    expect(screen.getByText('mobile').closest('ins')).not.toBeNull()
    // The shared words are not marked on either side.
    for (const shared of screen.getAllByText(/Fired when the user completes/)) {
      expect(shared.closest('del, ins')).toBeNull()
    }
  })
})

describe('BranchesTab — creating a branch and starting work on it', () => {
  function ActiveBranch() {
    return <output aria-label="active branch">{useActiveBranchId() ?? 'main'}</output>
  }

  function renderAt(path: string) {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={authAs('owner')}>
          <MemoryRouter initialEntries={[path]}>
            <BranchProvider slug="demo">
              <ActiveBranch />
              <Routes>
                <Route path="/p/:slug/branches" element={<BranchesTabRoute />} />
                <Route path="/p/:slug/branches/:branchId" element={<BranchesTabRoute />} />
              </Routes>
            </BranchProvider>
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )
  }

  it("opens the New branch dialog from the switcher's ?new=1", async () => {
    mockBranchDetailQueries([MAIN, FEATURE])
    renderAt('/p/demo/branches?new=1')

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('New branch')).toBeInTheDocument()
  })

  it('refuses a name with spaces, offers a usable one, and a taken one', async () => {
    mockBranchDetailQueries([MAIN, FEATURE])
    renderAt('/p/demo/branches?new=1')

    const dialog = await screen.findByRole('dialog')
    const name = within(dialog).getByLabelText('Name')
    fireEvent.change(name, { target: { value: 'Bad name with spaces!!' } })
    expect(within(dialog).getByText('Branch names cannot contain spaces.')).toBeInTheDocument()
    expect(name).toHaveAttribute('aria-invalid', 'true')

    fireEvent.click(within(dialog).getByRole('button', { name: 'Use bad-name-with-spaces' }))
    expect(name).toHaveValue('bad-name-with-spaces')
    expect(name).not.toHaveAttribute('aria-invalid')

    fireEvent.change(name, { target: { value: 'checkout-v2' } })
    expect(within(dialog).getByText('A branch with this name already exists.')).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create branch' }))
    expect(planBranchesApi.create).not.toHaveBeenCalled()
  })

  it('switches to the new branch by default and says so', async () => {
    mockBranchDetailQueries([MAIN])
    const created = makeBranch({ id: 'feat-new', name: 'paywall-copy', kind: 'working', status: 'draft' })
    vi.mocked(planBranchesApi.create).mockResolvedValue(created)
    const success = vi.spyOn(toast, 'success')
    renderAt('/p/demo/branches')

    // The empty project's pane teaches the flow and offers the first branch.
    expect(await screen.findByText('Propose plan changes safely')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Create a branch' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByLabelText('Switch to this branch now')).toBeChecked()
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'paywall-copy' } })
    // The refetch after the create lists it, as the server would.
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, created], total: 2 })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create branch' }))

    await waitFor(() => expect(screen.getByRole('status', { name: 'active branch' })).toHaveTextContent('feat-new'))
    expect(success).toHaveBeenCalledWith(expect.stringContaining('Switched to paywall-copy'))
  })
})

describe('BranchesTab "Update from main" header', () => {
  function behindDiff() {
    vi.mocked(planBranchesApi.diff).mockResolvedValue({
      behind_base: true,
      summary: { added: 0, removed: 0, changed: 1 },
      entries: [
        {
          entity_type: 'variable',
          kind: 'changed',
          name: 'plan',
          parent: null,
          changes: [],
          field_changes: [],
          before: null,
          after: null,
        },
      ],
    })
  }

  it('says safe to merge only on a confirmed answer, and offers the update', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({
      entities: [],
      unresolved_count: 0,
      behind: true,
      overlap_count: 0,
      merge_blocked: false,
      updatable: true,
    })
    behindDiff()

    renderTab('feat-1')

    expect(
      await screen.findByText('Main has newer changes to other entities — safe to merge.'),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Update from main' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Merge to main/ })).toBeInTheDocument()
  })

  it('leads the merge through the update when the merge would refuse', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({
      entities: [],
      unresolved_count: 0,
      behind: true,
      overlap_count: 0,
      merge_blocked: true,
      updatable: true,
    })
    behindDiff()

    renderTab('feat-1')

    expect(
      await screen.findByText(/Main changed entities this branch also changes/),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Update from main first/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Merge to main/ })).not.toBeInTheDocument()
  })

  it('never offers the update on a base that cannot take it', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })
    vi.mocked(planBranchesApi.listComments).mockResolvedValue([])
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue({
      entities: [],
      unresolved_count: 0,
      behind: true,
      overlap_count: 0,
      merge_blocked: true,
      updatable: false,
    })
    behindDiff()

    renderTab('feat-1')

    expect(await screen.findByText(/Copy your changes to a new branch/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Update from main/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Merge to main/ })).toBeInTheDocument()
  })
})
