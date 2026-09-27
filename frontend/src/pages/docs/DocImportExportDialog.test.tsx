import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { toast } from 'sonner'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import type { DocBundle, DocImportResult, DocTreeLimits } from '@/types/docs'
import { DocImportExportDialog } from './DocImportExportDialog'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), info: vi.fn(), error: vi.fn() },
}))
vi.mock('@/api/docs', () => ({
  docsApi: {
    exportZip: vi.fn(),
    exportJson: vi.fn(),
    importJson: vi.fn(),
    importZip: vi.fn(),
  },
}))

import { docsApi } from '@/api/docs'

const LIMITS: DocTreeLimits = {
  max_file_bytes: 262144,
  max_files_per_scope: 5000,
  max_bundle_files: 2000,
  max_bundle_bytes: 20971520,
}

function result(overrides: Partial<DocImportResult> = {}): DocImportResult {
  return {
    scope: 'project',
    mode: 'merge',
    dry_run: true,
    created: [],
    updated: [],
    unchanged: [],
    deleted: [],
    skipped: [],
    errors: [],
    ...overrides,
  }
}

function bundle(overrides: Partial<DocBundle> = {}): DocBundle {
  return {
    format: 'tripl-docs/v1',
    scope: 'project',
    project_slug: 'demo',
    organization_slug: 'acme',
    exported_at: '2026-09-01T00:00:00Z',
    files: [{ path: 'a.md', content: '# A', sha256: null }],
    ...overrides,
  }
}

function renderDialog(props: Partial<Parameters<typeof DocImportExportDialog>[0]> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  const onOpenChange = vi.fn()
  render(
    <QueryClientProvider client={client}>
      <DocImportExportDialog
        slug="demo"
        open
        onOpenChange={onOpenChange}
        canEdit
        isOwner={false}
        organizationName="Acme"
        limits={LIMITS}
        {...props}
      />
    </QueryClientProvider>,
  )
  return { invalidate, onOpenChange }
}

function jsonFile(content: unknown, name = 'bundle.json'): File {
  const text = typeof content === 'string' ? content : JSON.stringify(content)
  const f = new File([text], name, { type: 'application/json' })
  // jsdom's File has no text(); the dialog reads the upload through it.
  Object.defineProperty(f, 'text', { value: () => Promise.resolve(text) })
  return f
}

function pick(f: File) {
  fireEvent.change(screen.getByLabelText('File (.zip or .json)'), { target: { files: [f] } })
}

const preview = () => screen.getByRole('button', { name: /Preview \(dry run\)/ })
const apply = () => screen.getByRole('button', { name: /^Import$/ })

let createObjectURL: ReturnType<typeof vi.fn>
let clicked: string[]

beforeEach(() => {
  for (const fn of Object.values(docsApi)) vi.mocked(fn).mockReset()
  vi.mocked(toast.success).mockReset()
  createObjectURL = vi.fn(() => 'blob:x')
  Object.defineProperty(URL, 'createObjectURL', { value: createObjectURL, configurable: true })
  Object.defineProperty(URL, 'revokeObjectURL', { value: vi.fn(), configurable: true })
  clicked = []
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    clicked.push(this.download)
  })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('DocImportExportDialog export', () => {
  it('downloads the zip under the server-given filename', async () => {
    vi.mocked(docsApi.exportZip).mockResolvedValue({ blob: new Blob(['zip']), filename: 'demo-docs.zip' })
    renderDialog()
    fireEvent.click(screen.getByRole('button', { name: 'Download .zip' }))
    await waitFor(() => expect(clicked).toEqual(['demo-docs.zip']))
    expect(docsApi.exportZip).toHaveBeenCalledWith('demo', 'project')
    expect(createObjectURL).toHaveBeenCalled()
  })

  it('names a project JSON bundle after the project', async () => {
    vi.mocked(docsApi.exportJson).mockResolvedValue(bundle())
    renderDialog()
    fireEvent.click(screen.getByRole('button', { name: 'Download JSON bundle' }))
    await waitFor(() => expect(clicked).toEqual(['demo-docs.json']))
  })

  it('names an organization JSON bundle after the organization, else the project slug', async () => {
    vi.mocked(docsApi.exportJson)
      .mockResolvedValueOnce(bundle({ scope: 'organization' }))
      .mockResolvedValueOnce(bundle({ scope: 'organization', organization_slug: null }))
    renderDialog()
    fireEvent.click(screen.getByRole('button', { name: 'Organization · Acme' }))
    fireEvent.click(screen.getByRole('button', { name: 'Download JSON bundle' }))
    await waitFor(() => expect(clicked).toEqual(['acme-docs.json']))
    expect(docsApi.exportJson).toHaveBeenCalledWith('demo', 'organization')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Download JSON bundle' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Download JSON bundle' }))
    await waitFor(() => expect(clicked).toEqual(['acme-docs.json', 'demo-docs.json']))
  })

  it('shows an export failure in place', async () => {
    vi.mocked(docsApi.exportZip).mockRejectedValue(new ApiError('Export too large', 413))
    renderDialog()
    fireEvent.click(screen.getByRole('button', { name: 'Download .zip' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Export too large')
  })

  it('offers export but no import to a viewer', () => {
    renderDialog({ canEdit: false })
    expect(screen.getByRole('button', { name: 'Download .zip' })).toBeInTheDocument()
    expect(screen.queryByLabelText('File (.zip or .json)')).toBeNull()
    expect(screen.getByText(/Up to 2000 files/)).toBeInTheDocument()
  })

  it('closes from the footer', () => {
    const { onOpenChange } = renderDialog()
    // The footer button (the header's X shares the name).
    const buttons = screen.getAllByRole('button', { name: 'Close' })
    fireEvent.click(buttons.find(b => b.textContent === 'Close')!)
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })
})

describe('DocImportExportDialog import', () => {
  it('needs a file and a clean dry run before Import is enabled', async () => {
    vi.mocked(docsApi.importJson).mockResolvedValue(result({ created: ['a.md'] }))
    renderDialog()
    expect(preview()).toBeDisabled()
    expect(apply()).toBeDisabled()
    pick(jsonFile(bundle()))
    // A JSON bundle has no wrapper folder to keep.
    expect(screen.queryByText(/Keep the zip's top-level folder/)).toBeNull()
    expect(preview()).toBeEnabled()
    expect(apply()).toBeDisabled()

    fireEvent.click(preview())
    expect(await screen.findByText('Dry run — nothing was written')).toBeInTheDocument()
    expect(docsApi.importJson).toHaveBeenCalledWith(
      'demo',
      { scope: 'project', mode: 'merge', dryRun: true },
      { format: 'tripl-docs/v1', files: [{ path: 'a.md', content: '# A', sha256: null }] },
    )
    expect(apply()).toBeEnabled()
  })

  it('applies the import, refreshes the docs and reports the counts', async () => {
    vi.mocked(docsApi.importJson)
      .mockResolvedValueOnce(result({ created: ['a.md'] }))
      .mockResolvedValueOnce(result({ dry_run: false, created: ['a.md'], updated: ['b.md'] }))
    const { invalidate } = renderDialog()
    pick(jsonFile(bundle()))
    fireEvent.click(preview())
    await screen.findByText('Dry run — nothing was written')
    fireEvent.click(apply())
    expect(await screen.findByText('Imported')).toBeInTheDocument()
    expect(vi.mocked(docsApi.importJson).mock.calls[1]![1]).toEqual({ scope: 'project', mode: 'merge', dryRun: false })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['docs', 'demo'] })
    expect(toast.success).toHaveBeenCalledWith('Imported: 1 created, 1 updated, 0 deleted')
    // The applied result is not a dry run, so Import cannot be pressed twice.
    expect(apply()).toBeDisabled()
  })

  it('lists every outcome row and blocks Import while the preview has errors', async () => {
    vi.mocked(docsApi.importJson).mockResolvedValue(
      result({
        created: ['new.md'],
        updated: ['changed.md'],
        deleted: ['old.md'],
        unchanged: ['same.md'],
        skipped: [{ path: 'img.png', reason: 'not markdown' }],
        errors: [{ path: 'bad.md', detail: 'frontmatter is not YAML' }],
      }),
    )
    renderDialog()
    pick(jsonFile(bundle()))
    fireEvent.click(preview())
    await screen.findByText('Dry run — nothing was written')
    for (const label of ['Create', 'Update', 'Delete', 'Unchanged', 'Skipped', 'Error']) {
      expect(screen.getByRole('rowheader', { name: new RegExp(`^${label} 1`) })).toBeInTheDocument()
    }
    expect(screen.getByText('img.png — not markdown')).toBeInTheDocument()
    expect(screen.getByText('bad.md — frontmatter is not YAML')).toBeInTheDocument()
    expect(apply()).toBeDisabled()
  })

  it('omits empty outcome rows', async () => {
    vi.mocked(docsApi.importJson).mockResolvedValue(result({ unchanged: ['same.md'] }))
    renderDialog()
    pick(jsonFile(bundle()))
    fireEvent.click(preview())
    await screen.findByText('Dry run — nothing was written')
    expect(screen.getAllByRole('rowheader')).toHaveLength(1)
  })

  it('rejects a JSON file that is not a docs bundle without calling the server', async () => {
    renderDialog()
    pick(jsonFile({ hello: 'world' }))
    fireEvent.click(preview())
    expect(await screen.findByRole('alert')).toHaveTextContent('Not a tripl docs bundle')
    expect(docsApi.importJson).not.toHaveBeenCalled()
  })

  it('lists the per-file errors of a rejected (422) import', async () => {
    const err = new ApiError('Unprocessable', 422)
    err.detail = { errors: [{ path: 'a.md', detail: 'too big' }, { detail: 'no path' }] }
    vi.mocked(docsApi.importJson).mockRejectedValue(err)
    renderDialog()
    pick(jsonFile(bundle()))
    fireEvent.click(preview())
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Nothing was imported:')
    expect(alert).toHaveTextContent('a.md: too big')
    expect(alert).toHaveTextContent('?: no path')
    expect(alert).not.toHaveTextContent('more')
  })

  it('caps a long 422 error list at 20 lines', async () => {
    const err = new ApiError('Unprocessable', 422)
    err.detail = { errors: Array.from({ length: 23 }, (_, i) => ({ path: `f${i}.md`, detail: 'bad' })) }
    vi.mocked(docsApi.importJson).mockRejectedValue(err)
    renderDialog()
    pick(jsonFile(bundle()))
    fireEvent.click(preview())
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('f19.md: bad')
    expect(alert).not.toHaveTextContent('f20.md')
    expect(alert).toHaveTextContent('…and 3 more')
  })

  it('falls back to the plain message when a 422 has no error list', async () => {
    const err = new ApiError('Bundle is empty', 422)
    err.detail = { errors: [] }
    vi.mocked(docsApi.importJson).mockRejectedValue(err)
    renderDialog()
    pick(jsonFile(bundle()))
    fireEvent.click(preview())
    expect(await screen.findByRole('alert')).toHaveTextContent('Bundle is empty')
  })

  it('uploads a zip with the keep-root choice', async () => {
    vi.mocked(docsApi.importZip).mockResolvedValue(result({ created: ['SKILL.md'] }))
    renderDialog()
    const zip = new File(['PK'], 'skill.zip', { type: 'application/zip' })
    pick(zip)
    const keep = screen.getByRole('checkbox')
    fireEvent.click(keep)
    expect(keep).toBeChecked()
    fireEvent.click(preview())
    await screen.findByText('Dry run — nothing was written')
    expect(docsApi.importZip).toHaveBeenCalledWith(
      'demo',
      { scope: 'project', mode: 'merge', dryRun: true, keepRoot: true },
      zip,
    )
  })

  it('refuses a zip over the 10 MB cap before uploading it', async () => {
    renderDialog()
    const zip = new File(['PK'], 'huge.zip', { type: 'application/zip' })
    Object.defineProperty(zip, 'size', { value: 11 * 1024 * 1024 })
    pick(zip)
    fireEvent.click(preview())
    expect(await screen.findByRole('alert')).toHaveTextContent('the limit is 10 MB')
    expect(docsApi.importZip).not.toHaveBeenCalled()
  })

  it('clears a stale preview when the mode changes and warns what mirror deletes', async () => {
    vi.mocked(docsApi.importJson).mockResolvedValue(result({ created: ['a.md'] }))
    renderDialog()
    pick(jsonFile(bundle()))
    fireEvent.click(preview())
    await screen.findByText('Dry run — nothing was written')
    const mode = screen.getByRole('group', { name: 'Import mode' })
    fireEvent.click(within(mode).getByRole('button', { name: 'Mirror' }))
    expect(screen.queryByText('Dry run — nothing was written')).toBeNull()
    expect(screen.getByText(/Mirror deletes every project note/)).toBeInTheDocument()
    // Mirror on project notes is open to any editor.
    expect(preview()).toBeEnabled()
  })

  it('lets only the instance owner mirror organization notes', () => {
    renderDialog({ isOwner: false })
    pick(jsonFile(bundle()))
    fireEvent.click(screen.getByRole('button', { name: 'Organization · Acme' }))
    fireEvent.click(within(screen.getByRole('group', { name: 'Import mode' })).getByRole('button', { name: 'Mirror' }))
    expect(screen.getByText(/Mirror deletes every organization note/)).toHaveTextContent(
      'Only the instance owner can mirror organization notes.',
    )
    expect(preview()).toBeDisabled()
  })

  it('lets the owner mirror organization notes', async () => {
    vi.mocked(docsApi.importJson).mockResolvedValue(result({ scope: 'organization', mode: 'mirror', deleted: ['x.md'] }))
    renderDialog({ isOwner: true })
    pick(jsonFile(bundle()))
    fireEvent.click(screen.getByRole('button', { name: 'Organization · Acme' }))
    fireEvent.click(within(screen.getByRole('group', { name: 'Import mode' })).getByRole('button', { name: 'Mirror' }))
    expect(screen.getByText(/Mirror deletes every organization note/)).not.toHaveTextContent('Only the instance owner')
    expect(preview()).toBeEnabled()
    fireEvent.click(preview())
    await screen.findByText('Dry run — nothing was written')
    expect(vi.mocked(docsApi.importJson).mock.calls[0]![1]).toEqual({
      scope: 'organization',
      mode: 'mirror',
      dryRun: true,
    })
    expect(screen.getByRole('rowheader', { name: /^Delete 1/ })).toBeInTheDocument()
  })

  it('clears the pick when the file input is emptied', () => {
    renderDialog()
    pick(jsonFile(bundle()))
    expect(preview()).toBeEnabled()
    fireEvent.change(screen.getByLabelText('File (.zip or .json)'), { target: { files: [] } })
    expect(preview()).toBeDisabled()
  })
})
