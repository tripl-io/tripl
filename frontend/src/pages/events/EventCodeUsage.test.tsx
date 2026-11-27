import { render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { checkRunsApi } from '@/api/checkRuns'
import type { CheckRun, EventCodeUsage as Usage, EventCodeUsageRepository } from '@/types'
import { CHECK_UPLOAD_DOCS_URL } from '@/lib/docsSite'
import { expectNoAxeViolations } from '@/test/axe'
import { EventCodeUsage } from './EventCodeUsage'

vi.mock('@/api/checkRuns', () => ({
  checkRunsApi: { codeUsage: vi.fn() },
}))

function run(overrides: Partial<CheckRun> = {}): CheckRun {
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
    calls: { total: 2, ok: 1, warning: 1, error: 0, dynamic: 0 },
    created_at: new Date(Date.now() - 3 * 86_400_000).toISOString(),
    ...overrides,
  }
}

function repository(overrides: Partial<EventCodeUsageRepository> = {}): EventCodeUsageRepository {
  return {
    repository: 'shop/web',
    check_root: '.',
    repository_url: 'https://git.example.com/shop/web',
    run: run(),
    calls: [
      { path: 'src/cart.ts', line: 12, column: 5, status: 'ok', findings: [] },
      { path: 'src/checkout page.ts', line: 40, column: 1, status: 'warning', findings: [] },
    ],
    ...overrides,
  }
}

function usage(overrides: Partial<Usage> = {}): Usage {
  return { event_id: 'event-1', repositories: [repository()], code_url_template: null, ...overrides }
}

function renderBlock() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <EventCodeUsage slug="demo" eventId="event-1" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.mocked(checkRunsApi.codeUsage).mockReset()
})

describe('Used in code', () => {
  it('lists each place as plain text without a link template, and says where to set one', async () => {
    vi.mocked(checkRunsApi.codeUsage).mockResolvedValue(usage())
    renderBlock()

    expect(await screen.findByText('src/cart.ts:12')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Used in code' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'shop/web' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /src\/cart\.ts:12/ })).toBeNull()
    expect(screen.getByText('Warning')).toBeInTheDocument()
    // The snapshot's ref, commit and age.
    expect(screen.getByText('abcdef0')).toHaveAttribute('title', 'abcdef0123456789')
    expect(screen.getByText('3d ago')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Pipeline/ })).toHaveAttribute(
      'href',
      'https://git.example.com/shop/web/-/pipelines/7',
    )
    expect(screen.getByRole('link', { name: 'Settings › Check runs' })).toHaveAttribute(
      'href',
      '/settings/project/check-runs?project=demo',
    )
    expect(checkRunsApi.codeUsage).toHaveBeenCalledWith('demo', 'event-1', expect.anything())
  })

  it('has no axe violations, loaded or loading', async () => {
    vi.mocked(checkRunsApi.codeUsage).mockResolvedValue(usage())
    const { container } = renderBlock()
    await expectNoAxeViolations(container)
    await screen.findByText('src/cart.ts:12')
    await expectNoAxeViolations(container)
  })

  it('links each place through the template, escaping the path', async () => {
    vi.mocked(checkRunsApi.codeUsage).mockResolvedValue(
      usage({ code_url_template: 'https://github.com/{repository}/blob/{commit}/{path}#L{line}' }),
    )
    renderBlock()

    const link = await screen.findByRole('link', { name: /src\/checkout page\.ts:40/ })
    expect(link).toHaveAttribute(
      'href',
      'https://github.com/shop/web/blob/abcdef0123456789/src/checkout%20page.ts#L40',
    )
    expect(link).toHaveAttribute('target', '_blank')
    expect(screen.queryByRole('link', { name: 'Settings › Check runs' })).toBeNull()
  })

  it('names each check directory and says when one has no call of the event', async () => {
    vi.mocked(checkRunsApi.codeUsage).mockResolvedValue(
      usage({
        repositories: [
          repository({ check_root: 'apps/ios', calls: [] }),
          repository({ repository: 'shop/android', run: run({ pipeline_url: null }) }),
        ],
      }),
    )
    renderBlock()

    const iosHeading = await screen.findByRole('heading', { name: /shop\/web\s*·\s*apps\/ios/ })
    const ios = iosHeading.closest('section') as HTMLElement
    expect(ios).toHaveAttribute('aria-labelledby', iosHeading.id)
    expect(within(ios).getByText('No call sends this event here.')).toBeInTheDocument()
    const android = screen.getByRole('heading', { name: 'shop/android' }).closest('section') as HTMLElement
    expect(within(android).getByText('src/cart.ts:12')).toBeInTheDocument()
    expect(within(android).queryByRole('link', { name: /Pipeline/ })).toBeNull()
  })

  it('says when no run was ever uploaded, and links the docs on uploading', async () => {
    vi.mocked(checkRunsApi.codeUsage).mockResolvedValue(usage({ repositories: [] }))
    renderBlock()

    expect(await screen.findByText(/No check run from a default branch has been uploaded/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Upload check runs from CI/ })).toHaveAttribute(
      'href',
      CHECK_UPLOAD_DOCS_URL,
    )
  })

  it('shows a failed read with a retry', async () => {
    vi.mocked(checkRunsApi.codeUsage).mockRejectedValue(new Error('boom'))
    renderBlock()

    expect(await screen.findByText("Couldn't load where the code sends this event")).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Retry|Try again/ })).toBeInTheDocument()
  })

  it('lists two calls on the same line as two places', async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.mocked(checkRunsApi.codeUsage).mockResolvedValue(
      usage({
        repositories: [
          repository({
            calls: [
              { path: 'src/a.ts', line: 3, column: null, status: 'ok', findings: [] },
              { path: 'src/a.ts', line: 3, column: null, status: 'warning', findings: [] },
            ],
          }),
        ],
      }),
    )
    renderBlock()

    expect(await screen.findAllByText('src/a.ts:3')).toHaveLength(2)
    const duplicateKey = errors.mock.calls.some((args) => String(args[0]).includes('same key'))
    errors.mockRestore()
    expect(duplicateKey).toBe(false)
  })
})
