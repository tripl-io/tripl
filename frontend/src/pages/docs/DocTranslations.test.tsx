import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import type { DocFileResponse, DocTranslationSummary } from '@/types/docs'
import { DocLanguageBar, DocLanguagesDialog, DocTranslationNotice, TranslateDialog } from './DocTranslations'

vi.mock('@/api/docs', () => ({
  docsApi: {
    translate: vi.fn(),
    writeTranslation: vi.fn(),
    removeTranslation: vi.fn(),
    updateLanguages: vi.fn(),
    translationRevisions: vi.fn(),
    restoreTranslationRevision: vi.fn(),
  },
}))

import { docsApi } from '@/api/docs'

function translation(overrides: Partial<DocTranslationSummary> = {}): DocTranslationSummary {
  return {
    lang: 'de',
    status: 'ready',
    revision: 1,
    source_revision: 1,
    outdated: false,
    machine: true,
    error: '',
    updated_at: '2026-10-01T00:00:00Z',
    updated_by_name: null,
    ...overrides,
  }
}

function note(overrides: Partial<DocFileResponse> = {}): DocFileResponse {
  return {
    scope: 'project',
    path: 'a.md',
    title: 'A',
    description: '',
    tags: [],
    audience: 'both',
    revision: 1,
    size_bytes: 3,
    updated_at: '2026-10-01T00:00:00Z',
    updated_by_name: null,
    visibility: 'level',
    my_permission: 'edit',
    shared: false,
    id: 'd-1',
    content: '# A',
    body: '# A',
    extra_frontmatter: {},
    links: [],
    created_at: '2026-10-01T00:00:00Z',
    created_by_name: null,
    lang: null,
    requested_lang: null,
    translation_fallback: null,
    translation_outdated: false,
    translations: [],
    ...overrides,
  }
}

function wrap(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const view = render(<QueryClientProvider client={client}>{children}</QueryClientProvider>)
  return {
    ...view,
    rerender: (next: ReactNode) => view.rerender(<QueryClientProvider client={client}>{next}</QueryClientProvider>),
  }
}

beforeEach(() => {
  for (const fn of Object.values(docsApi)) vi.mocked(fn).mockReset()
})

describe('DocLanguageBar', () => {
  it('marks an AI translation and lets an editor delete it', async () => {
    vi.mocked(docsApi.removeTranslation).mockResolvedValue(undefined)
    const onChangeLang = vi.fn()
    const doc = note({ lang: 'de', translations: [translation()] })
    wrap(<DocLanguageBar slug="demo" doc={doc} lang="de" canEdit onChangeLang={onChangeLang} />)
    expect(screen.getByText('AI translation')).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Language' })).toHaveTextContent('German')
    fireEvent.click(screen.getByRole('button', { name: 'Delete the German translation' }))
    await waitFor(() => expect(onChangeLang).toHaveBeenCalledWith('original'))
    expect(docsApi.removeTranslation).toHaveBeenCalledWith('demo', 'project', 'a.md', 'de')
  })

  it('shows nothing to a reader of a note with no translations', () => {
    const { container } = wrap(
      <DocLanguageBar slug="demo" doc={note()} lang="original" canEdit={false} onChangeLang={vi.fn()} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('shows a reader the switch once there is a translation, without the editor controls', () => {
    const doc = note({ translations: [translation({ machine: false })] })
    wrap(<DocLanguageBar slug="demo" doc={doc} lang="original" canEdit={false} onChangeLang={vi.fn()} />)
    expect(screen.getByRole('combobox', { name: 'Language' })).toHaveTextContent('Original')
    expect(screen.queryByRole('button', { name: 'Translate with AI' })).toBeNull()
  })
})

describe('TranslateDialog', () => {
  it('asks which language and follows the code the server chose', async () => {
    vi.mocked(docsApi.translate).mockResolvedValue(translation({ status: 'pending', revision: 0 }))
    const onStarted = vi.fn()
    const onOpenChange = vi.fn()
    wrap(<TranslateDialog slug="demo" doc={note()} open onOpenChange={onOpenChange} onStarted={onStarted} />)
    fireEvent.change(screen.getByLabelText('Which language?'), { target: { value: 'немецкий' } })
    fireEvent.click(screen.getByRole('button', { name: 'Translate' }))
    await waitFor(() => expect(onStarted).toHaveBeenCalledWith('de'))
    expect(docsApi.translate).toHaveBeenCalledWith('demo', {
      scope: 'project',
      path: 'a.md',
      language: 'немецкий',
      overwrite: false,
    })
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('asks before replacing a translation someone edited', async () => {
    const refusal = new ApiError('conflict', 409)
    refusal.detail = { code: 'translation_edited', message: 'The de translation has been edited by hand.' }
    vi.mocked(docsApi.translate)
      .mockRejectedValueOnce(refusal)
      .mockResolvedValue(translation({ status: 'pending' }))
    const onStarted = vi.fn()
    wrap(<TranslateDialog slug="demo" doc={note()} open onOpenChange={vi.fn()} onStarted={onStarted} />)
    fireEvent.change(screen.getByLabelText('Which language?'), { target: { value: 'de' } })
    fireEvent.click(screen.getByRole('button', { name: 'Translate' }))
    expect(await screen.findByText('The de translation has been edited by hand.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Replace it' }))
    await waitFor(() => expect(onStarted).toHaveBeenCalledWith('de'))
    expect(vi.mocked(docsApi.translate).mock.calls[1]?.[1]).toMatchObject({ overwrite: true })
  })

  it('shows a refusal such as an unknown language in place', async () => {
    vi.mocked(docsApi.translate).mockRejectedValue(new ApiError("'banana' is not a language", 422))
    wrap(<TranslateDialog slug="demo" doc={note()} open onOpenChange={vi.fn()} onStarted={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Which language?'), { target: { value: 'banana' } })
    fireEvent.click(screen.getByRole('button', { name: 'Translate' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("'banana' is not a language")
  })
})

describe('DocTranslationNotice', () => {
  it('says a translation is behind the original and can mark it current', async () => {
    vi.mocked(docsApi.writeTranslation).mockResolvedValue(translation())
    const doc = note({
      revision: 3,
      lang: 'de',
      content: '# Hallo',
      translation_outdated: true,
      translations: [translation({ outdated: true, revision: 2, source_revision: 1 })],
    })
    wrap(<DocTranslationNotice slug="demo" doc={doc} canEdit onChangeLang={vi.fn()} />)
    expect(screen.getByText('This translation is behind the original')).toBeInTheDocument()
    expect(screen.getByText(/made from revision 1; the original is at revision 3/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Mark as up to date' }))
    await waitFor(() =>
      expect(docsApi.writeTranslation).toHaveBeenCalledWith('demo', {
        scope: 'project',
        path: 'a.md',
        lang: 'de',
        content: '# Hallo',
        base_revision: 2,
        mark_current: true,
      }),
    )
  })

  it('explains the original is shown while a translation is missing, failed or being made', () => {
    const missing = note({ requested_lang: 'de', translation_fallback: 'missing' })
    const { rerender } = wrap(<DocTranslationNotice slug="demo" doc={missing} canEdit onChangeLang={vi.fn()} />)
    expect(screen.getByText('There is no German translation of this note yet')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Translate into German with AI' })).toBeInTheDocument()

    const failed = note({
      requested_lang: 'de',
      translation_fallback: 'failed',
      translations: [translation({ status: 'failed', revision: 0, error: 'The AI provider request failed' })],
    })
    rerender(<DocTranslationNotice slug="demo" doc={failed} canEdit onChangeLang={vi.fn()} />)
    expect(screen.getByText(/The AI provider request failed/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()

    const pending = note({ requested_lang: 'de', translation_fallback: 'pending' })
    rerender(<DocTranslationNotice slug="demo" doc={pending} canEdit onChangeLang={vi.fn()} />)
    expect(screen.getByText('Translating into German…')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  })
})

describe('DocTranslationNotice while translating again', () => {
  it('shows the run in progress instead of actions the server would refuse', () => {
    const doc = note({
      revision: 3,
      lang: 'de',
      translation_outdated: true,
      translations: [translation({ status: 'pending', outdated: true, revision: 2, source_revision: 1 })],
    })
    wrap(<DocTranslationNotice slug="demo" doc={doc} canEdit onChangeLang={vi.fn()} />)
    expect(screen.getByText('Translating into German again…')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Mark as up to date' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Translate again' })).toBeNull()
  })
})

describe('DocLanguagesDialog', () => {
  it('saves both defaults, empty meaning the original', async () => {
    vi.mocked(docsApi.updateLanguages).mockResolvedValue({ agent_lang: 'en', human_lang: null })
    const onOpenChange = vi.fn()
    wrap(
      <DocLanguagesDialog
        slug="demo"
        defaults={{ agent_lang: null, human_lang: 'ru' }}
        open
        onOpenChange={onOpenChange}
        canEdit
      />,
    )
    fireEvent.change(screen.getByLabelText('For agents (API, MCP, CLI)'), { target: { value: 'en' } })
    expect(screen.getByText(/^English\./)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('For people (this app)'), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(docsApi.updateLanguages).toHaveBeenCalledWith('demo', { agent_lang: 'en', human_lang: null }),
    )
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('is read-only for someone who cannot edit the project', () => {
    wrap(
      <DocLanguagesDialog
        slug="demo"
        defaults={{ agent_lang: 'en', human_lang: null }}
        open
        onOpenChange={vi.fn()}
        canEdit={false}
      />,
    )
    expect(screen.getByLabelText('For agents (API, MCP, CLI)')).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })
})
