import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiKeysApi } from '@/api/apiKeys'
import { projectsApi } from '@/api/projects'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import type { ApiKey } from '@/types'
import ApiKeysSection from './ApiKeysSection'
import { isKeyInactive } from './apiKeyStatus'

function ownerAuthValue(): AuthContextValue {
  return {
    user: {
      id: 'owner-1',
      email: 'owner@example.com',
      name: 'owner',
      role: 'owner',
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

function key(overrides: Partial<ApiKey> & { id: string; name: string }): ApiKey {
  return {
    key_prefix: 'trpl_abc',
    scope: 'read',
    project_id: null,
    expires_at: null,
    revoked_at: null,
    last_used_at: null,
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderSection() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={ownerAuthValue()}>
        <MemoryRouter initialEntries={['/settings/workspace/api-keys']}>
          <ApiKeysSection />
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ApiKeysSection', () => {
  it('reveals the New API key form above the Active keys card on "Create key"', async () => {
    vi.spyOn(apiKeysApi, 'list').mockResolvedValue([])
    vi.spyOn(projectsApi, 'list').mockResolvedValue([])

    renderSection()

    expect(screen.queryByText('New API key')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Create key/i }))

    const formTitle = await screen.findByText('New API key')
    const keyListTitle = screen.getByText('Your keys')

    // The form must render above the key list card so it appears right where
    // the user clicked, not below the fold (regression).
    expect(
      formTitle.compareDocumentPosition(keyListTitle) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()

    // The revealed form focuses the key-name input immediately.
    expect(screen.getByLabelText('Name')).toHaveFocus()
  })

  // an empty name is flagged inline on Generate, not by a browser bubble.
  it('says an empty key name is required, inline', async () => {
    vi.spyOn(apiKeysApi, 'list').mockResolvedValue([])
    vi.spyOn(projectsApi, 'list').mockResolvedValue([])
    const create = vi.spyOn(apiKeysApi, 'create')

    renderSection()
    fireEvent.click(screen.getByRole('button', { name: /Create key/i }))
    await screen.findByText('New API key')
    fireEvent.click(screen.getByRole('button', { name: 'Generate' }))

    const name = screen.getByLabelText(/^Name/)
    expect(name).not.toHaveAttribute('required')
    expect(name).toHaveAttribute('aria-invalid', 'true')
    expect(await screen.findByRole('alert')).toHaveTextContent('Required')
    expect(create).not.toHaveBeenCalled()
  })

  // The card used to headline "Active keys · 10 keys" from the unfiltered list,
  // so revoked and expired tokens were counted as live ones.
  it('counts only usable keys in the card heading and names the dead ones', async () => {
    vi.spyOn(apiKeysApi, 'list').mockResolvedValue([
      key({ id: 'k1', name: 'codex' }),
      key({ id: 'k2', name: 'claude' }),
      key({ id: 'k3', name: 'ro', revoked_at: '2026-05-01T00:00:00Z' }),
      key({ id: 'k4', name: 'admin', revoked_at: '2026-05-02T00:00:00Z' }),
      key({ id: 'k5', name: 'stale', expires_at: '2026-01-01T00:00:00Z' }),
    ])
    vi.spyOn(projectsApi, 'list').mockResolvedValue([])

    renderSection()

    expect(await screen.findByText('2 active · 3 revoked or expired')).toBeInTheDocument()
    expect(screen.queryByText('Active keys')).not.toBeInTheDocument()
    expect(screen.queryByText('5 keys')).not.toBeInTheDocument()
  })

  it('folds revoked keys away behind a count', async () => {
    vi.spyOn(apiKeysApi, 'list').mockResolvedValue([
      key({ id: 'k1', name: 'codex' }),
      key({ id: 'k3', name: 'ro', revoked_at: '2026-05-01T12:00:00Z' }),
    ])
    vi.spyOn(projectsApi, 'list').mockResolvedValue([])
    renderSection()

    expect(await screen.findByText('codex')).toBeInTheDocument()
    expect(screen.queryByText('ro')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Show revoked (1)' }))
    expect(screen.getByText('ro')).toBeInTheDocument()
    expect(screen.getByText('revoked 2026-05-01')).toBeInTheDocument()
  })

  it('teaches what a key is for when there are none', async () => {
    vi.spyOn(apiKeysApi, 'list').mockResolvedValue([])
    vi.spyOn(projectsApi, 'list').mockResolvedValue([])
    renderSection()

    expect(await screen.findByRole('heading', { name: 'No API keys yet' })).toBeInTheDocument()
    expect(screen.queryByText(/shown in full only once/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Create your first key' }))
    expect(await screen.findByText('New API key')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Create key/ })).toBeDisabled()
  })

  it('says there are no active keys when every key is revoked, not a bare card', async () => {
    vi.spyOn(apiKeysApi, 'list').mockResolvedValue([
      key({ id: 'k3', name: 'ro', revoked_at: '2026-05-01T12:00:00Z' }),
    ])
    vi.spyOn(projectsApi, 'list').mockResolvedValue([])
    renderSection()

    expect(await screen.findByRole('heading', { name: 'No active keys' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Show revoked (1)' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Show revoked (1)' }))
    expect(screen.getByText('ro')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'No active keys' })).toBeNull()
  })

  // a failed load fell through to "No API keys yet".
  it('shows an error with retry, not an empty list, when the keys fail to load', async () => {
    vi.spyOn(apiKeysApi, 'list').mockRejectedValue(new Error('Server exploded'))
    vi.spyOn(projectsApi, 'list').mockResolvedValue([])

    renderSection()

    expect(await screen.findByText("Couldn't load API keys")).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
    expect(screen.queryByText(/No API keys yet/)).not.toBeInTheDocument()
    expect(screen.queryByText(/0 active/)).not.toBeInTheDocument()
  })

  // active keys show when they expire, using the inclusive rule.
  it('shows the expiry date of active keys', async () => {
    vi.spyOn(apiKeysApi, 'list').mockResolvedValue([
      key({ id: 'k1', name: 'expiring', expires_at: '2999-06-15T12:00:00Z' }),
      key({ id: 'k2', name: 'forever' }),
    ])
    vi.spyOn(projectsApi, 'list').mockResolvedValue([])

    renderSection()

    expect(await screen.findByText('expires 2999-06-15')).toBeInTheDocument()
    expect(screen.getByText('no expiry')).toBeInTheDocument()
  })

  // the single line keys off the row's width, not the viewport's, so the
  // narrow settings column at 768px keeps the stacked layout.
  it('switches a key row to one line by container width, not viewport', async () => {
    vi.spyOn(apiKeysApi, 'list').mockResolvedValue([key({ id: 'k1', name: 'codex' })])
    vi.spyOn(projectsApi, 'list').mockResolvedValue([])

    renderSection()

    const revoke = await screen.findByRole('button', { name: 'Revoke codex' })
    const row = revoke.parentElement as HTMLElement
    expect(row).toHaveClass('grid', '@min-[560px]:flex')
    expect(row.className).not.toMatch(/(^|\s)sm:/)
    expect(row.parentElement).toHaveClass('@container')
  })

  // revoke failures were silent and one pending revoke disabled every row.
  it('scopes the pending revoke to its row and reports a failed revoke', async () => {
    vi.spyOn(apiKeysApi, 'list').mockResolvedValue([
      key({ id: 'k1', name: 'codex' }),
      key({ id: 'k2', name: 'claude' }),
    ])
    vi.spyOn(projectsApi, 'list').mockResolvedValue([])
    let rejectRevoke: (reason: unknown) => void = () => {}
    vi.spyOn(apiKeysApi, 'revoke').mockImplementation(
      () =>
        new Promise<never>((_, reject) => {
          rejectRevoke = reject
        }),
    )

    renderSection()

    fireEvent.click(await screen.findByRole('button', { name: 'Revoke codex' }))
    const confirmDialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(confirmDialog).getByRole('button', { name: 'Revoke' }))

    const pending = await screen.findByRole('button', { name: 'Revoking… codex' })
    expect(pending).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Revoke claude' })).toBeEnabled()

    rejectRevoke(new Error('Forbidden'))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Couldn\'t revoke "codex" — it is still active.')
    expect(alert).toHaveTextContent('Forbidden')
    expect(screen.getByRole('button', { name: 'Revoke codex' })).toBeEnabled()
  })

  // Review 208: with only the target row disabled, a second revoke could start
  // while the first was in flight, and the first one's failure then vanished.
  it('keeps each concurrent revoke pending and reports the one that failed', async () => {
    vi.spyOn(apiKeysApi, 'list').mockResolvedValue([
      key({ id: 'k1', name: 'codex' }),
      key({ id: 'k2', name: 'claude' }),
    ])
    vi.spyOn(projectsApi, 'list').mockResolvedValue([])
    const rejects = new Map<string, (reason: unknown) => void>()
    vi.spyOn(apiKeysApi, 'revoke').mockImplementation(
      (keyId: string) =>
        new Promise<never>((_, reject) => {
          rejects.set(keyId, reject)
        }),
    )

    renderSection()

    for (const name of ['codex', 'claude']) {
      fireEvent.click(await screen.findByRole('button', { name: `Revoke ${name}` }))
      const confirmDialog = await screen.findByRole('alertdialog')
      fireEvent.click(within(confirmDialog).getByRole('button', { name: 'Revoke' }))
      await screen.findByRole('button', { name: `Revoking… ${name}` })
    }
    // The first row is still pending after the second revoke started.
    expect(screen.getByRole('button', { name: 'Revoking… codex' })).toBeDisabled()

    rejects.get('k1')?.(new Error('Forbidden'))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Couldn\'t revoke "codex" — it is still active.',
    )
    expect(screen.getByRole('button', { name: 'Revoke codex' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Revoking… claude' })).toBeDisabled()
  })

  // Cancel used to keep the abandoned draft and its error.
  it('clears the draft and the error when the form is cancelled', async () => {
    vi.spyOn(apiKeysApi, 'list').mockResolvedValue([])
    vi.spyOn(projectsApi, 'list').mockResolvedValue([])
    vi.spyOn(apiKeysApi, 'create').mockRejectedValue(new Error('Name already taken'))

    renderSection()

    fireEvent.click(screen.getByRole('button', { name: /Create key/i }))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'dup' } })
    fireEvent.click(screen.getByRole('button', { name: 'Generate' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Name already taken')

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: /Create key/i }))

    expect(await screen.findByLabelText('Name')).toHaveValue('')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  // the scope and project pickers are named kit selects.
  it('labels the scope and project selects', async () => {
    vi.spyOn(apiKeysApi, 'list').mockResolvedValue([])
    vi.spyOn(projectsApi, 'list').mockResolvedValue([])

    renderSection()
    fireEvent.click(screen.getByRole('button', { name: /Create key/i }))

    expect(await screen.findByRole('combobox', { name: 'Scope' })).toHaveValue('read')
    expect(screen.getByRole('combobox', { name: 'Project (optional)' })).toHaveValue('')
  })

  describe('one-time token reveal', () => {
    const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard')

    afterEach(() => {
      if (clipboardDescriptor) Object.defineProperty(navigator, 'clipboard', clipboardDescriptor)
      else Reflect.deleteProperty(navigator, 'clipboard')
    })

    async function mintKey() {
      vi.spyOn(apiKeysApi, 'list').mockResolvedValue([])
      vi.spyOn(projectsApi, 'list').mockResolvedValue([])
      vi.spyOn(apiKeysApi, 'create').mockResolvedValue({
        ...key({ id: 'k9', name: 'agent' }),
        token: 'trpl_secret_token',
      })
      renderSection()
      fireEvent.click(screen.getByRole('button', { name: /Create key/i }))
      fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'agent' } })
      fireEvent.click(screen.getByRole('button', { name: 'Generate' }))
      return screen.findByRole('dialog', { name: 'Copy your API key now' })
    }

    // Esc or an outside click discarded a token that is never shown again.
    it('stays open on Escape and closes only through its own button', async () => {
      const dialog = await mintKey()

      fireEvent.keyDown(dialog, { key: 'Escape' })
      expect(screen.getByRole('dialog', { name: 'Copy your API key now' })).toBeInTheDocument()
      expect(within(dialog).queryByRole('button', { name: 'Close' })).not.toBeInTheDocument()
      // Says which key and how to send it.
      expect(within(dialog).getByText('agent · read-only · All projects · no expiry')).toBeInTheDocument()
      expect(within(dialog).getByText('Authorization: Bearer <key>')).toBeInTheDocument()

      // Not copied yet: closing is the reader's claim to have saved it.
      fireEvent.click(within(dialog).getByRole('button', { name: 'I’ve saved it' }))
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    })

    // navigator.clipboard is undefined over plain HTTP; the copy threw.
    it('selects the token and says so when the clipboard is unavailable', async () => {
      Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
      const dialog = await mintKey()
      const token = within(dialog).getByRole('textbox', { name: 'API key' })
      expect(token).toHaveValue('trpl_secret_token')

      fireEvent.click(within(dialog).getByRole('button', { name: 'Copy' }))

      expect(await within(dialog).findByRole('alert')).toHaveTextContent(
        /Couldn’t reach the clipboard/,
      )
      expect(token).toHaveFocus()
    })

    // a browser that refuses the write is a failure too, not a silent "Copied".
    it('reports a refused clipboard write and selects the token', async () => {
      const writeText = vi.fn().mockRejectedValue(new Error('NotAllowedError'))
      Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
      const dialog = await mintKey()

      fireEvent.click(within(dialog).getByRole('button', { name: 'Copy' }))

      expect(await within(dialog).findByRole('alert')).toHaveTextContent(
        /Couldn’t reach the clipboard/,
      )
      expect(within(dialog).queryByRole('button', { name: 'Copied' })).toBeNull()
      expect(within(dialog).getByRole('textbox', { name: 'API key' })).toHaveFocus()
    })

    it('confirms a successful copy', async () => {
      const writeText = vi.fn().mockResolvedValue(undefined)
      Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
      const dialog = await mintKey()

      fireEvent.click(within(dialog).getByRole('button', { name: 'Copy' }))

      expect(await within(dialog).findByRole('button', { name: 'Copied' })).toBeInTheDocument()
      expect(writeText).toHaveBeenCalledWith('trpl_secret_token')
      expect(within(dialog).getByRole('button', { name: 'Done' })).toBeInTheDocument()
    })
  })
})

describe('isKeyInactive', () => {
  const at = (iso: string) => new Date(iso)

  it('counts a key as inactive at the exact expiry instant, matching the backend', () => {
    // The backend rejects a token once expires_at <= now
    // (backend/src/tripl/services/api_key_service.py:125). A strict `<` here
    // labelled the key active for that instant.
    const expiring = { revoked_at: null, expires_at: '2026-06-01T12:00:00Z' }
    expect(isKeyInactive(expiring, at('2026-06-01T12:00:00Z'))).toBe(true)
    expect(isKeyInactive(expiring, at('2026-06-01T11:59:59Z'))).toBe(false)
    expect(isKeyInactive(expiring, at('2026-06-01T12:00:01Z'))).toBe(true)
  })

  it('treats a revoked key as inactive regardless of expiry', () => {
    expect(isKeyInactive({ revoked_at: '2026-05-01T00:00:00Z', expires_at: null })).toBe(true)
  })

  it('treats a key with no expiry as active', () => {
    expect(isKeyInactive({ revoked_at: null, expires_at: null })).toBe(false)
  })
})
