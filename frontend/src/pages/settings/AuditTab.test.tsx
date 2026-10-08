import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { AuditActionCatalog, AuditEntry, AuditEntryDetail, AuditListResponse } from '@/types'
import { ApiError } from '@/api/client'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { authAs } from '@/test/auth'

// The audit endpoints are stubbed so the tab renders without firing a real
// request; each test decides what the page it asks for contains, and what the
// one-entry payload read behind an expanded row answers.
const { listMock, getMock, actionsMock } = vi.hoisted(() => ({
  listMock: vi.fn(),
  getMock: vi.fn(),
  actionsMock: vi.fn(),
}))

vi.mock('@/api/audit', () => ({
  projectAuditSource: (slug: string) => ({
    key: `project:${slug}`,
    wide: false,
    list: listMock,
    get: getMock,
    actions: actionsMock,
  }),
}))

// Rows name the actor from the roster, falling back to the email.
vi.mock('@/api/users', () => ({
  usersApi: {
    list: vi.fn(async () => [
      { id: 'u-alice', email: 'alice@example.com', name: 'Alice Moreau', role: 'owner', created_at: '2026-01-01T00:00:00Z' },
    ]),
  },
}))

import { AuditLog, AuditTab } from './AuditTab'
import { pickDate } from '@/test/pickers'

// What GET /audit/actions answers. The vocabulary is the backend's now:
// which actions carry a project is decided where they are recorded, so these
// tests pin how the page USES the two halves, not what is in them.
const CATALOG: AuditActionCatalog = {
  project: [
    { label: 'Events', actions: ['event.create', 'event.delete'] },
    { label: 'Project', actions: ['project.create', 'api_key.create'] },
  ],
  workspace: [{ label: 'Workspace', actions: ['data_source.create', 'project.delete'] }],
}

beforeEach(() => {
  listMock.mockReset()
  listMock.mockResolvedValue({ items: [], total: 0 })
  getMock.mockReset()
  actionsMock.mockReset()
  actionsMock.mockResolvedValue(CATALOG)
})

// `/audit` is owner-only, and the tab now says so to anyone else instead of
// asking, so every render is an owner's unless a test says otherwise.
function renderTab(auth: AuthContextValue | null = authAs('owner')) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <MemoryRouter>
          <AuditTab slug="demo" />
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

/**
 * One list row. Carries no payload — the list response does not have one.
 *
 * `branchName` defaults to '' because that is the common row: a write to main,
 * or an action with no plan-branch dimension at all.
 */
function auditRow(index: number, branchName = ''): AuditEntry {
  return {
    id: `entry-${index}`,
    created_at: '2026-08-17T10:00:00Z',
    user_id: null,
    user_email: 'alice@example.com',
    project_id: null,
    project_slug: 'demo',
    branch_id: branchName ? `branch-${index}` : null,
    branch_name: branchName,
    action: 'event_type.update',
    target_type: 'event_type',
    target_id: null,
    target_name: `checkout_started_${index}`,
  }
}

/** One page of `size` rows out of `total` — what any page but the last looks like. */
function auditPage(size: number, total: number): AuditListResponse {
  return { items: Array.from({ length: size }, (_, index) => auditRow(index)), total }
}

/** What `GET /audit/{id}` answers for row `index`: the same row, plus its payload. */
function auditDetail(index: number, payload: Record<string, unknown>): AuditEntryDetail {
  return { ...auditRow(index), payload }
}

/** Every action the Action <select> offers, in DOM order (minus "Action: any"). */
function offeredActions(): string[] {
  const select = screen.getByLabelText('Action') as HTMLSelectElement
  return Array.from(select.querySelectorAll('option'))
    .map((option) => option.value)
    .filter((value) => value !== '')
}

describe('AuditTab — events in the log', () => {
  // api/v1/events.py called audit_service.record zero times, so the central
  // object of the product was the one object this filter had nothing to offer
  // for. Per-event history is not a substitute: it never records creation or
  // deletion and CASCADEs away with the event it documents.
  it('describes a log that covers events and outlives the event it records', () => {
    renderTab()

    const help = screen.getByText(/Compliance trail/)
    // The paragraph scoped itself to "schema and data sources" — true before
    // this change, and a false promise the moment events started landing here.
    expect(help.textContent).not.toMatch(/schema and data sources/i)
    expect(help.textContent).toMatch(/events/i)
    // Points across to the per-event history instead of pretending this log
    // carries field-level before/after values, which it deliberately does not.
    expect(help.textContent).toMatch(/history/i)
    // The two facts the branch-chip test below also depends on survive the
    // rewrite; asserting them here too so a copy edit fails in the test that
    // owns the copy.
    expect(help.textContent).not.toMatch(/no chip were written on main/i)
    expect(help.textContent).toMatch(/no branch to name/i)
  })
})

/** An organization-wide log, as the Enterprise edition mounts one. */
const WIDE_SOURCE = {
  key: 'org',
  wide: true,
  list: (...args: unknown[]) => listMock(...args),
  get: (...args: unknown[]) => getMock(...args),
  actions: () => actionsMock(),
}

function WorkspaceAuditLog() {
  return <AuditLog source={WIDE_SOURCE} />
}

describe('AuditLog — an organization-wide source', () => {
  function renderWorkspace() {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={authAs('owner')}>
          <MemoryRouter>
            <WorkspaceAuditLog />
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )
  }

  it('names the project each row belongs to, since rows from all of them sit together', async () => {
    listMock.mockResolvedValue({ items: [auditRow(0)], total: 1 })
    renderWorkspace()

    expect(await screen.findByTitle('demo')).toBeInTheDocument()
  })

  it('does not repeat the project chip inside one project', async () => {
    listMock.mockResolvedValue({ items: [auditRow(0)], total: 1 })
    renderTab()

    await screen.findByText('checkout_started_0')
    // Every row on that page belongs to the project whose page it is, so the
    // chip would restate the heading on every line.
    expect(screen.queryByTitle('demo')).toBeNull()
  })
})

describe('AuditTab — date filters', () => {
  it('filters with the app calendar, not a native date field', () => {
    renderTab()

    // The native <input type="date"> rendered in the BROWSER's locale and
    // theme (mm/dd/yyyy on a US profile), unlike every other date in the app.
    expect(screen.queryByText('(YYYY-MM-DD)')).toBeNull()
    expect(document.querySelector('input[type="date"]')).toBeNull()
    expect(screen.getByLabelText('From')).toHaveAccessibleName('From: none picked')
    expect(screen.getByLabelText('To')).toHaveAccessibleName('To: none picked')
  })
})

describe('AuditTab — paging', () => {
  it('asks for one 50-row page and offers a step past it', async () => {
    listMock.mockResolvedValue(auditPage(50, 254))
    renderTab()

    // The page used to request the endpoint's own 200 ceiling and send no
    // offset, so those 200 rows were the only rows reachable at all.
    await waitFor(() =>
      expect(listMock).toHaveBeenCalledWith(expect.objectContaining({ limit: 50, offset: 0 })),
    )
    expect(await screen.findByRole('button', { name: 'Older' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Newer' })).toBeDisabled()
    // The dead end it replaced: the only route to row 201 was guessing an
    // action type or a date range.
    expect(screen.queryByText(/narrow the filter to drill into older actions/)).toBeNull()
  })

  it('steps Older and Newer by exactly one page', async () => {
    listMock.mockResolvedValue(auditPage(50, 254))
    renderTab()

    fireEvent.click(await screen.findByRole('button', { name: 'Older' }))

    await waitFor(() =>
      expect(listMock).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 50 })),
    )
    expect(await screen.findByText('Showing 51–100 of 254 entries.')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Newer' }))

    expect(
      await screen.findByText(/Showing the most recent 50 of 254 entries/),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Newer' })).toBeDisabled()
  })

  it('returns to the newest page whenever a filter is written', async () => {
    listMock.mockResolvedValue(auditPage(50, 254))
    renderTab()

    fireEvent.click(await screen.findByRole('button', { name: 'Older' }))
    await waitFor(() =>
      expect(listMock).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 50 })),
    )

    // The offset indexes INTO the filtered set, so narrowing 254 entries to a
    // handful while parked on page 2 would land on a blank page of a list that
    // has rows — which reads as "nothing matches".
    // An option the select holds only once the vocabulary has arrived.
    await screen.findByRole('option', { name: 'Deleted event' })
    fireEvent.change(screen.getByLabelText('Action'), {
      target: { value: 'event.delete' },
    })

    await waitFor(() =>
      expect(listMock).toHaveBeenLastCalledWith(
        expect.objectContaining({ action: 'event.delete', offset: 0 }),
      ),
    )
  })

  it('says the next page is loading and refuses a second step until it lands', async () => {
    listMock.mockResolvedValue(auditPage(50, 254))
    renderTab()

    const older = await screen.findByRole('button', { name: 'Older' })
    let releasePage2: (value: AuditListResponse) => void = () => {}
    listMock.mockReturnValueOnce(
      new Promise<AuditListResponse>((resolve) => {
        releasePage2 = resolve
      }),
    )

    fireEvent.click(older)
    await waitFor(() =>
      expect(listMock).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 50 })),
    )

    // keepPreviousData holds page 1 on screen for the whole round trip, so the
    // caption has to keep describing page 1: it used to read "Showing 51–100"
    // above rows 1–50, i.e. name rows the list was not showing.
    expect(screen.getByText(/Showing the most recent 50 of 254 entries/)).toBeInTheDocument()
    expect(screen.getByText('Updating…')).toBeInTheDocument()

    // Nothing on screen changed, so the obvious reaction is to click again. That
    // moved the key to offset 100 and the offset-50 response was dropped
    // unrendered — rows 51–100 unreachable, with no sign a page was skipped.
    expect(screen.getByRole('button', { name: 'Older' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Newer' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Older' }))
    expect(listMock).not.toHaveBeenCalledWith(expect.objectContaining({ offset: 100 }))

    releasePage2(auditPage(50, 254))

    expect(await screen.findByText('Showing 51–100 of 254 entries.')).toBeInTheDocument()
    expect(screen.queryByText('Updating…')).toBeNull()
  })

  it('hides the pager when one page holds everything', async () => {
    listMock.mockResolvedValue(auditPage(3, 3))
    renderTab()

    await screen.findByText('checkout_started_0')
    expect(screen.queryByRole('button', { name: 'Older' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Newer' })).toBeNull()
  })
})

describe('AuditTab — pending list card', () => {
  it('holds the shape of the list instead of a bare "Loading…" line', async () => {
    let release: (value: AuditListResponse) => void = () => {}
    listMock.mockReturnValue(
      new Promise<AuditListResponse>((resolve) => {
        release = resolve
      }),
    )
    renderTab()

    // The header and the whole filter card render immediately; only this card
    // is pending, and a one-line placeholder made it look empty rather than
    // about to be a list.
    expect(screen.getByLabelText('Loading audit entries')).toBeInTheDocument()
    expect(screen.queryByText('Loading…')).toBeNull()

    release(auditPage(1, 1))
    await waitFor(() =>
      expect(screen.queryByLabelText('Loading audit entries')).not.toBeInTheDocument(),
    )
  })
})

describe('AuditTab — payload on expand', () => {
  it('reads a payload only for the row the reader expanded', async () => {
    listMock.mockResolvedValue(auditPage(3, 3))
    getMock.mockResolvedValue(auditDetail(1, { sensitivity: 'pii' }))
    renderTab()

    // The list used to carry a payload for every row while the tab rendered one
    // only for expanded rows: a page of JSON blobs on the wire to display none.
    await screen.findByText('checkout_started_1')
    expect(getMock).not.toHaveBeenCalled()

    fireEvent.click(screen.getByText('checkout_started_1'))

    expect(await screen.findByText(/"sensitivity": "pii"/)).toBeInTheDocument()
    expect(getMock).toHaveBeenCalledTimes(1)
    expect(getMock).toHaveBeenCalledWith('entry-1')
  })

  it('leaves a row whose payload is empty looking exactly as it did', async () => {
    listMock.mockResolvedValue(auditPage(1, 1))
    // A bulk inbox mute files one row per incident with `{}`, and expanding one
    // showed the header line and nothing else.
    getMock.mockResolvedValue(auditDetail(0, {}))
    const { container } = renderTab()

    fireEvent.click(await screen.findByText('checkout_started_0'))

    await waitFor(() => expect(getMock).toHaveBeenCalledWith('entry-0'))
    await waitFor(() => expect(screen.queryByLabelText('Loading payload')).toBeNull())
    expect(container.querySelector('pre')).toBeNull()
  })
})

describe('AuditTab — branch chip', () => {
  it('names the working branch a write was scoped to', async () => {
    listMock.mockResolvedValue({ items: [auditRow(1, 'redesign-checkout')], total: 1 })
    renderTab()

    // Before this, two contradictory edits to the same object on two branches
    // produced two audit rows that read identically.
    expect(await screen.findByText('redesign-checkout')).toBeInTheDocument()
    // The chip is capped and truncated, so the full name has to stay reachable.
    expect(screen.getByTitle('redesign-checkout')).toBeInTheDocument()
  })

  it('leaves a row with no branch unchipped rather than calling it main', async () => {
    // An empty branch_name covers BOTH a write to main and an action with no
    // plan-branch dimension at all (alerting, scans, metrics, API keys — all
    // listed in this tab's own filter), so labelling it "main" would assert
    // something false about the second kind.
    listMock.mockResolvedValue({
      items: [auditRow(0), auditRow(1, 'redesign-checkout')],
      total: 2,
    })
    renderTab()

    await screen.findByText('checkout_started_0')
    expect(screen.getByText('checkout_started_1')).toBeInTheDocument()
    // Both rows rendered; exactly one of them carries a chip.
    expect(screen.getAllByTitle('redesign-checkout')).toHaveLength(1)
    expect(screen.queryByText('main')).toBeNull()

    // …and the tab's own help text must not say it for us. The line above
    // cannot catch that: queryByText matches an element's whole normalized
    // text exactly, and the help paragraph is a long sentence, so it passed
    // while the copy read "entries with no chip were written on main" —
    // false for every alerting, scan, metric and API-key row this tab lists.
    const help = screen.getByText(/Compliance trail/)
    expect(help.textContent).not.toMatch(/no chip were written on main/i)
    expect(help.textContent).toMatch(/no branch to name/i)
  })
})

describe('AuditTab — who may read it, and a failed read', () => {
  it('tells an editor the log is owner-only instead of claiming it is empty', () => {
    renderTab(authAs('member'))

    expect(screen.getByText('Only owners can read the audit log')).toBeInTheDocument()
    expect(screen.queryByText(/No audit entries yet/)).toBeNull()
    expect(listMock).not.toHaveBeenCalled()
  })

  it('renders a 403 as owner-only, not as an empty log', async () => {
    listMock.mockRejectedValue(new ApiError('Owner role required', 403))
    renderTab()

    expect(await screen.findByText('Only owners can read the audit log')).toBeInTheDocument()
    expect(screen.queryByText(/No audit entries yet/)).toBeNull()
  })

  it('renders a 500 as an error with a retry', async () => {
    listMock.mockRejectedValue(new ApiError('Internal error', 500))
    renderTab()

    expect(await screen.findByText("Couldn't load the audit log")).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(screen.queryByText(/No audit entries yet/)).toBeNull()
  })
})

describe('AuditTab — rows and filters', () => {
  it('says whether a row is expanded', async () => {
    listMock.mockResolvedValue(auditPage(1, 1))
    getMock.mockResolvedValue(auditDetail(0, { a: 1 }))
    renderTab()

    const row = (await screen.findByText('checkout_started_0')).closest('button') as HTMLElement
    expect(row).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(row)
    expect(row).toHaveAttribute('aria-expanded', 'true')
    const controlled = row.getAttribute('aria-controls')
    expect(controlled).toBeTruthy()
    expect(document.getElementById(controlled as string)).not.toBeNull()
  })

  it('offers no day before From for To, so a backwards range cannot be picked', async () => {
    renderTab()
    await waitFor(() => expect(listMock).toHaveBeenCalledTimes(1))

    await pickDate('From', '2026-08-20')
    await waitFor(() => expect(screen.queryByRole('grid')).toBeNull())
    expect(screen.getByLabelText('From')).toHaveAttribute('data-value', '2026-08-20')

    // Open To on August, where From's day sits.
    await pickDate('To', '2026-08-25')
    fireEvent.click(screen.getByLabelText('To'))
    const grid = await screen.findByRole('grid', { name: 'August 2026' })
    expect(within(grid).getByRole('button', { name: 'Monday, August 10, 2026' })).toBeDisabled()
    expect(within(grid).getByRole('button', { name: 'Thursday, August 20, 2026' })).toBeEnabled()
    expect(screen.queryByRole('alert')).toBeNull()
    await waitFor(() => expect(listMock).toHaveBeenCalledTimes(3))
    expect(listMock.mock.calls.some(([params]) => params.until !== undefined)).toBe(true)
  })

  it('applies the email filter after a pause, without Enter', async () => {
    renderTab()
    await waitFor(() => expect(listMock).toHaveBeenCalledTimes(1))

    fireEvent.change(screen.getByLabelText('User email contains'), { target: { value: 'alice' } })

    await waitFor(() =>
      expect(listMock).toHaveBeenLastCalledWith(expect.objectContaining({ userEmail: 'alice' })),
    )
  })
})

describe('AuditTab — the action vocabulary comes from the backend', () => {
  it('offers the project half, grouped, in a project', async () => {
    renderTab()

    await waitFor(() => expect(offeredActions()).toEqual([
      'event.create',
      'event.delete',
      'project.create',
      'api_key.create',
    ]))
    const select = screen.getByLabelText('Action') as HTMLSelectElement
    const labels = Array.from(select.querySelectorAll('optgroup')).map((group) => group.label)
    expect(labels).toEqual(['Events', 'Project'])
  })

  it('offers both halves in the workspace feed', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={authAs('owner')}>
          <MemoryRouter>
            <WorkspaceAuditLog />
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )

    await waitFor(() => expect(offeredActions()).toContain('project.delete'))
    expect(offeredActions()).toContain('event.create')
  })

  it('offers only "Action: any" until the vocabulary has loaded', () => {
    actionsMock.mockReturnValue(new Promise(() => {}))
    renderTab()

    const select = screen.getByLabelText('Action') as HTMLSelectElement
    expect(Array.from(select.querySelectorAll('option')).map((o) => o.textContent)).toEqual(['Action: any'])
  })

  it('labels each action as the row chip reads, with the code only where two read alike', async () => {
    actionsMock.mockResolvedValue({
      project: [{ label: 'Events', actions: ['event.create', 'event.delete', 'event.bulk_delete'] }],
      workspace: [],
    })
    renderTab()

    await waitFor(() => expect(offeredActions()).toHaveLength(3))
    const select = screen.getByLabelText('Action') as HTMLSelectElement
    const options = Array.from(select.querySelectorAll('option')).filter((o) => o.value !== '')
    expect(options.map((o) => [o.value, o.textContent])).toEqual([
      ['event.create', 'Created event'],
      ['event.delete', 'Deleted event (event.delete)'],
      ['event.bulk_delete', 'Deleted event (event.bulk_delete)'],
    ])
  })
})

describe('AuditTab — rows read as sentences', () => {
  it('says what happened in words, names the person, and groups rows by day', async () => {
    listMock.mockResolvedValue({
      items: [
        {
          ...auditRow(0, 'redesign-checkout'),
          user_id: 'u-alice',
          action: 'plan_branch.approve',
          target_type: 'plan_branch',
          target_id: 'b-9',
          target_name: 'redesign-checkout',
          project_id: 'p-1',
        },
      ],
      total: 1,
    })
    getMock.mockResolvedValue({
      ...auditRow(0),
      payload: { status: 'approved' },
    })
    renderTab()

    const chip = await screen.findByText('Approved branch')
    // The code stays reachable for whoever filters by it: on the chip, whose
    // sentence sits in an inner span so it can truncate.
    expect(chip.closest('[data-slot="chip"]')).toHaveAttribute('title', 'plan_branch.approve')
    expect(await screen.findByText('Alice Moreau')).toHaveAttribute('title', 'alice@example.com')
    expect(screen.getByRole('region', { name: 'Aug 17, 2026' })).toBeInTheDocument()

    fireEvent.click(chip.closest('button') as HTMLElement)
    // The payload as labelled values, the raw JSON folded away, and links to
    // the target and its branch.
    expect(await screen.findByText('Status')).toBeInTheDocument()
    expect(screen.getByText('approved')).toBeInTheDocument()
    expect(screen.getByText('Raw JSON')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Open branch$/ })).toHaveAttribute(
      'href',
      '/p/demo/branches/b-9',
    )
    expect(screen.getByRole('link', { name: /Open branch redesign-checkout/ })).toHaveAttribute(
      'href',
      '/p/demo/branches/branch-0',
    )
  })

  it('keeps the header to one line and the details under "About this log"', () => {
    renderTab()

    expect(screen.getByText("Every change to this project's plan, scans, metrics and alerting.")).toBeInTheDocument()
    expect(screen.getByText('About this log')).toBeInTheDocument()
  })
})
