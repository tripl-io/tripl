import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { checkRunsApi } from '@/api/checkRuns'
import { projectsApi } from '@/api/projects'
import { AuthContext } from '@/components/auth-context'
import { authAs } from '@/test/auth'
import { expectNoAxeViolations } from '@/test/axe'
import type { CheckRun, Project } from '@/types'
import { CHECK_UPLOAD_DOCS_URL } from '@/lib/docsSite'
import CheckRunsSection from './CheckRunsSection'

vi.mock('@/api/projects', () => ({
  projectsApi: { get: vi.fn(), list: vi.fn() },
}))
vi.mock('@/api/checkRuns', () => ({
  checkRunsApi: {
    list: vi.fn(),
    directories: vi.fn(),
    getSettings: vi.fn(),
    updateSettings: vi.fn(),
  },
}))

function project(canMutate: boolean): Project {
  return {
    id: 'project-1',
    name: 'Demo',
    slug: 'demo',
    description: '',
    app_version_keep_releases: 5,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    my_role: canMutate ? 'editor' : 'viewer',
    can_mutate: canMutate,
  } as Project
}

function run(overrides: Partial<CheckRun>): CheckRun {
  return {
    id: 'run-1',
    repository: 'shop/web',
    check_root: '.',
    repository_url: 'https://git.example.com/shop/web',
    commit_sha: 'abcdef0123456789',
    ref: 'main',
    default_branch: 'main',
    merge_request: false,
    is_default_branch: true,
    is_current: true,
    pipeline_url: 'https://git.example.com/shop/web/-/pipelines/7',
    pipeline_created_at: null,
    committed_at: null,
    strict: false,
    calls: { total: 12, ok: 9, warning: 2, error: 1, dynamic: 0 },
    created_at: '2026-11-20T10:00:00Z',
    ...overrides,
  }
}

const RUNS = [
  run({ id: 'run-current' }),
  run({
    id: 'run-mr',
    ref: 'feature/cart',
    merge_request: true,
    is_default_branch: false,
    is_current: false,
    commit_sha: '1234567890abcdef',
    pipeline_url: null,
    calls: { total: 3, ok: 3, warning: 0, error: 0, dynamic: 0 },
  }),
  run({ id: 'run-ios', repository: 'shop/apps', check_root: 'apps/ios', is_current: true }),
]

function renderSection(canMutate = true, slug = 'demo') {
  vi.mocked(projectsApi.get).mockResolvedValue(project(canMutate))
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const tree = (current: string) => (
    <QueryClientProvider client={client}>
      <AuthContext.Provider value={authAs('member', 'u-1')}>
        <MemoryRouter>
          <CheckRunsSection slug={current} />
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>
  )
  const view = render(tree(slug))
  return { ...view, switchTo: (next: string) => view.rerender(tree(next)) }
}

beforeEach(() => {
  vi.mocked(checkRunsApi.list).mockReset().mockResolvedValue({ items: RUNS, total: RUNS.length })
  vi.mocked(checkRunsApi.directories).mockReset().mockResolvedValue({
    items: [
      { repository: 'shop/apps', check_root: 'apps/ios', runs: 1, last_run_at: '2026-11-20T10:00:00Z' },
      { repository: 'shop/web', check_root: '.', runs: 2, last_run_at: '2026-11-20T10:00:00Z' },
    ],
  })
  vi.mocked(checkRunsApi.getSettings).mockReset().mockResolvedValue({ code_url_template: null })
  vi.mocked(checkRunsApi.updateSettings).mockReset()
})

describe('Project · Check runs', () => {
  it('lists the uploaded runs with their ref, commit, counts and whether each is current', async () => {
    renderSection()

    expect(screen.getByRole('heading', { name: 'Check runs', level: 1 })).toBeInTheDocument()
    const table = await screen.findByRole('table')
    const rows = within(table).getAllByRole('row')
    expect(rows).toHaveLength(RUNS.length + 1)
    const current = rows[1] as HTMLElement
    const mergeRequest = rows[2] as HTMLElement
    const ios = rows[3] as HTMLElement

    expect(within(current).getByText('shop/web')).toBeInTheDocument()
    expect(within(current).getByText('Default branch')).toBeInTheDocument()
    expect(within(current).getByText('abcdef0')).toHaveAttribute('title', 'abcdef0123456789')
    expect(within(current).getByRole('link', { name: /Pipeline/ })).toHaveAttribute(
      'href',
      'https://git.example.com/shop/web/-/pipelines/7',
    )
    expect(within(current).getByText('12')).toBeInTheDocument()
    expect(within(current).getByText('1 error, 2 warnings')).toBeInTheDocument()
    expect(within(current).getByText('Current')).toBeInTheDocument()

    expect(within(mergeRequest).getByText('feature/cart')).toBeInTheDocument()
    expect(within(mergeRequest).getByText('Merge request')).toBeInTheDocument()
    expect(within(mergeRequest).getByText('History only')).toBeInTheDocument()
    expect(within(mergeRequest).getByText('None')).toBeInTheDocument()

    expect(within(ios).getByText('apps/ios')).toBeInTheDocument()
    expect(screen.getByText('3 runs, newest first.')).toBeInTheDocument()
  })

  it('has no axe violations, loaded or loading', async () => {
    const { container } = renderSection()
    await expectNoAxeViolations(container)
    await screen.findByRole('table')
    await screen.findByLabelText('Repository URL template')
    await expectNoAxeViolations(container)
  })

  it('filters the history by repository and check directory', async () => {
    renderSection()
    await screen.findByRole('table')

    fireEvent.change(await screen.findByLabelText('Repository'), { target: { value: 'shop/apps' } })
    await waitFor(() =>
      expect(checkRunsApi.list).toHaveBeenLastCalledWith(
        'demo',
        { repository: 'shop/apps', checkRoot: undefined, offset: 0, limit: 50 },
        expect.anything(),
      ),
    )
    const directory = screen.getByLabelText('Check directory')
    // Only the directories of the picked repository are offered.
    expect(within(directory).getAllByRole('option').map((option) => option.textContent)).toEqual([
      'All check directories',
      'apps/ios',
    ])
    fireEvent.change(directory, { target: { value: 'apps/ios' } })
    await waitFor(() =>
      expect(checkRunsApi.list).toHaveBeenLastCalledWith(
        'demo',
        { repository: 'shop/apps', checkRoot: 'apps/ios', offset: 0, limit: 50 },
        expect.anything(),
      ),
    )
  })

  it('lets an editor set the link template, and shows an example link', async () => {
    const template = 'https://github.com/{repository}/blob/{commit}/{path}#L{line}'
    vi.mocked(checkRunsApi.updateSettings).mockResolvedValue({ code_url_template: template })
    renderSection(true)

    const input = await screen.findByLabelText('Repository URL template')
    fireEvent.change(input, { target: { value: template } })
    expect(
      screen.getByText(
        'https://github.com/org/app/blob/0123456789abcdef0123456789abcdef01234567/src/checkout/Cart.ts#L42',
      ),
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))

    await waitFor(() =>
      expect(checkRunsApi.updateSettings).toHaveBeenCalledWith('demo', { code_url_template: template }),
    )
    expect(await screen.findByText('Saved')).toBeInTheDocument()
  })

  it('shows why the server refused a template', async () => {
    vi.mocked(checkRunsApi.updateSettings).mockRejectedValue(
      new Error('must start with http:// or https:// and a host, or with {repository_url}'),
    )
    renderSection(true)

    fireEvent.change(await screen.findByLabelText('Repository URL template'), {
      target: { value: 'git.example.com/{path}' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))

    expect(await screen.findByText(/must start with http:\/\/ or https:\/\//)).toBeInTheDocument()
  })

  it('shows a viewer the template read-only, with no save bar', async () => {
    vi.mocked(checkRunsApi.getSettings).mockResolvedValue({
      code_url_template: 'https://git.example.com/{path}',
    })
    renderSection(false)

    const input = await screen.findByLabelText('Repository URL template')
    await waitFor(() => expect(input).toHaveValue('https://git.example.com/{path}'))
    await waitFor(() => expect(input).toHaveAttribute('readonly'))
    expect(screen.queryByRole('button', { name: /Save changes/ })).toBeNull()
  })

  it('starts afresh on another project: no draft template and no filters carry over', async () => {
    const { switchTo } = renderSection(true, 'alpha')
    await screen.findByRole('table')
    fireEvent.change(await screen.findByLabelText('Repository URL template'), {
      target: { value: 'https://alpha.example.com/{path}' },
    })
    fireEvent.change(screen.getByLabelText('Repository'), { target: { value: 'shop/web' } })
    await waitFor(() =>
      expect(checkRunsApi.list).toHaveBeenLastCalledWith(
        'alpha',
        { repository: 'shop/web', checkRoot: undefined, offset: 0, limit: 50 },
        expect.anything(),
      ),
    )

    switchTo('beta')

    await waitFor(() =>
      expect(checkRunsApi.list).toHaveBeenLastCalledWith(
        'beta',
        { repository: undefined, checkRoot: undefined, offset: 0, limit: 50 },
        expect.anything(),
      ),
    )
    await waitFor(() => expect(screen.getByLabelText('Repository URL template')).toHaveValue(''))
    expect(screen.getByLabelText('Repository')).toHaveValue('')
    expect(screen.queryByText('Unsaved changes')).toBeNull()
  })

  it('steps back to the last page when the page shown no longer has runs', async () => {
    const full = Array.from({ length: 50 }, (_, index) => run({ id: `run-${index}` }))
    vi.mocked(checkRunsApi.list).mockImplementation(async (_slug, params) =>
      // The 51st run was cleaned up between the two pages.
      (params?.offset ?? 0) === 0 ? { items: full, total: 51 } : { items: [], total: 50 },
    )
    renderSection()
    await screen.findByRole('table')

    fireEvent.click(screen.getByRole('button', { name: /Older/ }))

    await waitFor(() =>
      expect(checkRunsApi.list).toHaveBeenLastCalledWith(
        'demo',
        { repository: undefined, checkRoot: undefined, offset: 0, limit: 50 },
        expect.anything(),
      ),
    )
    expect(await screen.findByRole('table')).toBeInTheDocument()
    expect(screen.queryByText('No check runs yet')).toBeNull()
  })

  it('says how to upload when no run was ever uploaded', async () => {
    vi.mocked(checkRunsApi.list).mockResolvedValue({ items: [], total: 0 })
    vi.mocked(checkRunsApi.directories).mockResolvedValue({ items: [] })
    renderSection()

    expect(await screen.findByText('No check runs yet')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /How to upload check runs/ })).toHaveAttribute(
      'href',
      CHECK_UPLOAD_DOCS_URL,
    )
    expect(screen.queryByLabelText('Repository')).toBeNull()
  })
})
