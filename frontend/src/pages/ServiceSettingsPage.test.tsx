import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { serviceSettingsApi } from '@/api/serviceSettings'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import type { ServiceSettings } from '@/types'
import ServiceSettingsSection from './ServiceSettingsPage'
import { RESET_FIELDS } from './settings-service/serviceSettingsHelpers'

/**
 * One override per section, so the reset card is live in the tests that click
 * it. A section with nothing overridden has nothing to reset, and its button is
 * disabled on purpose (tripl-5qp9) — see the reset-card describe below.
 */
const OVERRIDDEN_SOURCES: ServiceSettings['sources'] = {
  'runtime.app_base_url': 'override',
  'security.registration_mode': 'override',
  'storage.photo_max_size_mb': 'override',
  'observability.log_level': 'override',
  'email.smtp_host': 'override',
  'ai.ai_model': 'override',
}

function ownerAuthValue(): AuthContextValue {
  return {
    user: {
      id: 'owner-1',
      email: 'owner@example.com',
      name: 'owner',
      role: 'owner',
      is_platform_admin: true,
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

const SETTINGS = {
  runtime: { app_base_url: '', scan_row_limit_default: 100, metrics_row_limit_default: 100 },
  security: {
    registration_mode: 'open',
    cors_allow_origins: '',
    session_cookie_name: 'tripl_session',
    session_ttl_hours: 168,
    session_cookie_secure: false,
    security_headers_enabled: true,
    hsts_enabled: false,
    hsts_max_age_seconds: 31536000,
    content_security_policy: '',
    rate_limit_enabled: true,
    rate_limit_login_per_minute: 5,
    rate_limit_register_per_hour: 3,
    rate_limit_trust_forwarded_for: false,
  },
  storage: {
    photo_storage_backend: 'local',
    photo_local_dir: './var/photos',
    photo_max_size_mb: 10,
    photo_allowed_mime: 'image/png',
    gcs_photo_bucket: '',
    gcs_photo_credentials_path: '',
    gcs_photo_public: false,
    gcs_photo_signed_url_ttl_seconds: 3600,
  },
  observability: {
    request_id_header: 'X-Request-ID',
    log_level: 'INFO',
    log_json: false,
    prometheus_metrics_enabled: false,
    otel_exporter_otlp_endpoint: '',
    otel_service_name: 'tripl',
  },
  email: {
    smtp_host: '',
    smtp_port: 587,
    smtp_username: '',
    smtp_password_configured: true,
    smtp_security: 'starttls',
    smtp_from_address: '',
  },
  ai: {
    ai_enabled: false,
    ai_base_url: '',
    ai_model: '',
    ai_api_key_configured: true,
    ai_timeout_seconds: 30,
    ai_max_output_tokens: 700,
    describe_system_prompt: '',
    ask_system_prompt: '',
    alert_explanation_system_prompt: '',
    search_embeddings_enabled: false,
    search_embedding_provider: 'openai',
    search_embedding_model: '',
    search_embedding_api_key_configured: false,
    search_embedding_dimensions: 1536,
    search_embedding_base_url: 'https://api.openai.com/v1',
  },
  system: {
    debug: false,
    database_url_configured: true,
    sync_database_url_configured: true,
    rabbitmq_url_configured: true,
    redis_url_configured: false,
    encryption_key_configured: true,
    openai_api_key_configured: false,
    alembic_revision: 'abc123def456',
    alembic_head_revision: 'abc123def456',
    alembic_up_to_date: true,
  },
  overridden_fields: [],
  sources: OVERRIDDEN_SOURCES,
} as ServiceSettings

function renderSection(
  section: 'ai' | 'security' | 'storage' | 'email',
  sources: ServiceSettings['sources'] = OVERRIDDEN_SOURCES,
) {
  vi.spyOn(serviceSettingsApi, 'get').mockResolvedValue({ ...SETTINGS, sources })
  // spyOn hands back the SAME spy for a property already spied, so without this
  // the call history accumulates across tests and "not.toHaveBeenCalled" reads
  // the previous test's reset.
  const update = vi.spyOn(serviceSettingsApi, 'update').mockResolvedValue(SETTINGS)
  update.mockClear()
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={ownerAuthValue()}>
        <ServiceSettingsSection section={section} />
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
  return update
}

describe('Instance settings destructive actions', () => {
  /**
   * Both of these used to call the mutation straight from onClick: one click
   * nulled 13 overrides (including registration_mode, which applies live) or
   * deleted a stored key server-side, with no confirmation and no undo
   * (tripl-ifiy).
   */
  it('confirms before resetting a section, naming the section and its field count', async () => {
    const update = renderSection('security')

    fireEvent.click(await screen.findByRole('button', { name: /Reset to defaults/ }))

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(`Clear all ${RESET_FIELDS.security.length} Security & access`)
    expect(dialog).toHaveTextContent(/Self-service registration/)
    expect(update).not.toHaveBeenCalled()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Reset section' }))

    await waitFor(() => expect(update).toHaveBeenCalledTimes(1))
    expect(update).toHaveBeenCalledWith({
      security: Object.fromEntries(RESET_FIELDS.security.map((field) => [field, null])),
    })
  })

  it('does not promise the AI prompts an environment variable that cannot exist', async () => {
    // The dialog names "all three system prompts" in its own stakes line, and
    // none of the three has an environment variable — a reset returns them to a
    // built-in constant. Said in front of an irreversible write (tripl-wkwv.2).
    renderSection('ai')

    fireEvent.click(await screen.findByRole('button', { name: /Reset to defaults/ }))

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(/all three system prompts/i)
    expect(dialog).toHaveTextContent(/built-in default where none is set/i)
  })

  it('abandons the reset when the confirmation is declined', async () => {
    const update = renderSection('storage')

    fireEvent.click(await screen.findByRole('button', { name: /Reset to defaults/ }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(update).not.toHaveBeenCalled()
  })

  it('confirms before a per-secret Clear deletes the stored key', async () => {
    const update = renderSection('ai')

    const clearButtons = await screen.findAllByRole('button', { name: 'Delete stored key' })
    fireEvent.click(clearButtons[0]!)

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Delete the stored AI API key')
    // The field beside the button waits for Save changes; this does not.
    expect(dialog).toHaveTextContent(/does not wait for Save changes/)
    expect(update).not.toHaveBeenCalled()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }))

    await waitFor(() => expect(update).toHaveBeenCalledWith({ ai: { ai_api_key: null } }))
  })
})

describe('Instance settings write-through vs the unsaved draft', () => {
  /**
   * `form` spans all six sections and switching between two instance sections
   * keeps this component mounted, so the draft the leave-guard protects
   * (tripl-l8v2) is exactly what Reset and Clear used to overwrite: both adopted
   * the whole settings response, and the response cannot contain an edit that
   * was never sent.
   */
  function renderInstanceSettings(section: 'ai' | 'email' | 'security') {
    vi.spyOn(serviceSettingsApi, 'get').mockResolvedValue(SETTINGS)
    const update = vi.spyOn(serviceSettingsApi, 'update').mockResolvedValue(SETTINGS)
    update.mockClear()
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const tree = (current: 'ai' | 'email' | 'security') => (
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={ownerAuthValue()}>
          <ServiceSettingsSection section={current} />
        </AuthContext.Provider>
      </QueryClientProvider>
    )
    const view = render(tree(section))
    return {
      update,
      showSection: (next: 'ai' | 'email' | 'security') => view.rerender(tree(next)),
    }
  }

  const PROMPT = 'You answer in German.'

  it('keeps an unsaved prompt when another section is reset', async () => {
    const { update, showSection } = renderInstanceSettings('ai')

    fireEvent.change(await screen.findByLabelText('Ask prompt'), { target: { value: PROMPT } })
    // AI → Email keeps this component mounted, which is why the draft is still
    // alive when the Email reset writes through.
    showSection('email')

    fireEvent.click(await screen.findByRole('button', { name: /Reset to defaults/ }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Reset section' }))
    await waitFor(() => expect(update).toHaveBeenCalledTimes(1))

    showSection('ai')
    expect(await screen.findByLabelText('Ask prompt')).toHaveValue(PROMPT)
  })

  it('keeps an unsaved prompt when a stored secret is cleared', async () => {
    const { update } = renderInstanceSettings('ai')

    fireEvent.change(await screen.findByLabelText('Ask prompt'), { target: { value: PROMPT } })

    fireEvent.click((await screen.findAllByRole('button', { name: 'Delete stored key' }))[0]!)
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(update).toHaveBeenCalledWith({ ai: { ai_api_key: null } }))

    expect(screen.getByLabelText('Ask prompt')).toHaveValue(PROMPT)
  })

  // WS-23: Save used to send the whole form, so an edit left behind in another
  // section went out with this one and nothing on screen said so.
  it('saves only the section on screen and names the other unsaved sections', async () => {
    const { update, showSection } = renderInstanceSettings('security')

    fireEvent.change(await screen.findByLabelText('Login limit'), { target: { value: '9' } })
    showSection('ai')
    fireEvent.change(await screen.findByLabelText('Ask prompt'), { target: { value: PROMPT } })

    expect(screen.getByText(/Also unsaved: Security & access/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))
    await waitFor(() => expect(update).toHaveBeenCalledTimes(1))
    expect(update).toHaveBeenCalledWith({ ai: { ask_system_prompt: PROMPT } })

    // The Security edit is still a draft, not saved and not dropped.
    showSection('security')
    expect(await screen.findByLabelText('Login limit')).toHaveValue(9)
    expect(screen.getByRole('button', { name: /Save changes/ })).toBeEnabled()
  })

  it('names the unsaved edits a reset of this section does drop', async () => {
    renderInstanceSettings('ai')

    fireEvent.change(await screen.findByLabelText('Ask prompt'), { target: { value: PROMPT } })
    fireEvent.click(await screen.findByRole('button', { name: /Reset to defaults/ }))

    // The confirm enumerates every other consequence; it may not stay silent
    // about the one the user can see on screen.
    expect(await screen.findByRole('alertdialog')).toHaveTextContent(
      /Changes you made in AI but have not saved are dropped as well/,
    )
  })
})

describe('Instance settings reset card', () => {
  /**
   * The card interpolated RESET_FIELDS[section].length — how many fields a reset
   * is ABLE to null — and called them overrides. On a fresh instance, where
   * every row above it carries the outline "Env" badge, it therefore claimed in
   * red that there were 6 Email overrides to clear, next to a live button whose
   * PATCH would have changed nothing (tripl-5qp9).
   */
  it('counts the overrides that exist, not the fields it could reset', async () => {
    renderSection('email', { 'email.smtp_host': 'override', 'email.smtp_port': 'override' })

    expect(await screen.findByText(/Clears the 2 Email overrides/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Reset to defaults/ })).toBeEnabled()
  })

  it('agrees with the badges when a single field is overridden', async () => {
    renderSection('email', { 'email.smtp_host': 'override' })

    expect(await screen.findByText(/Clears the 1 Email override on this instance/)).toBeInTheDocument()
  })

  it('offers no reset card at all when nothing in the section is overridden (ST-29)', async () => {
    renderSection('email', {})

    await screen.findByRole('button', { name: 'Discard' })
    expect(screen.queryByRole('button', { name: /Reset to defaults/ })).toBeNull()
    expect(screen.queryByText(/Nothing to clear/)).toBeNull()
    // The "applies immediately" warning belongs to an action that can happen.
    expect(screen.queryByText(/does not wait for Save changes/)).toBeNull()
  })

  /**
   * The card's copy had claimed every non-overridden field "comes from an
   * environment variable" — the copy-disagrees-with-badges bug tripl-5qp9 was,
   * in a second vocabulary (tripl-wkwv.2). Unbadged rows are now explained once,
   * by the page legend, and it makes the same weaker claim.
   */
  it('does not claim the environment delivered fields it cannot vouch for', async () => {
    renderSection('email', {})

    const legend = await screen.findByText(/Fields marked Override are stored here/)
    expect(legend).toHaveTextContent(/built-in default where none is set/i)
    expect(screen.queryByText('Env')).toBeNull()
    // A value at its default carries no badge at all (ST-25).
    expect(screen.queryByText('Default')).toBeNull()
  })
})

describe('Instance settings source badges', () => {
  /**
   * `SettingSource` was `env | override`, computed as "override if a stored row
   * exists, else env" — so a field nothing had ever delivered was still badged
   * "Env", and the badge was not evidence of anything (tripl-wkwv.2).
   */
  it('separates a delivered value from one that merely equals the built-in default', async () => {
    renderSection('email', { 'email.smtp_host': 'default', 'email.smtp_port': 'env' })

    expect(await screen.findByText('Env')).toBeInTheDocument()
    // Only the one field the server said was delivered.
    expect(screen.getAllByText('Env')).toHaveLength(1)
    // The rest stay unbadged rather than wearing a grey "Default" each (ST-25).
    expect(screen.queryByText('Default')).toBeNull()
  })
})

describe('Instance settings save row', () => {
  it('states when this section’s overrides start being obeyed', async () => {
    renderSection('storage')

    // Storage is startup-applied; the page used to say nothing at all, while
    // Runtime — which is read per request — carried the redeploy note
    // (tripl-tezn).
    expect(await screen.findByText(/after the next restart/i)).toBeInTheDocument()
  })

  /**
   * The note promised "Unset fields fall back to environment variables" two
   * lines above the three AI prompt rows — and those three have no environment
   * variable at all: the backend reads them off ai_defaults, never off Settings,
   * so DESCRIBE_SYSTEM_PROMPT is silently ignored if anyone sets it. The badges
   * gained a third state in this same change; this sentence never caught up
   * (tripl-wkwv.2).
   */
  it('does not promise an environment fallback for fields that have no variable', async () => {
    renderSection('ai')

    const legend = await screen.findByText(/Fields marked Override are stored here/)
    expect(legend).toHaveTextContent(/built-in default where none is set/i)
  })

  it('keeps the sticky note to one line about when saving takes effect (ST-28)', async () => {
    renderSection('ai')

    const note = await screen.findByText(/no restart needed/i)
    expect(note).toHaveTextContent('Applies to the next AI call — no restart needed.')
    expect(note).not.toHaveTextContent(/fall back/i)
  })

  it('offers Discard beside Save and keeps both reachable while the pane scrolls', async () => {
    renderSection('ai')

    const discard = await screen.findByRole('button', { name: 'Discard' })
    expect(discard).toBeDisabled()

    const row = discard.closest('div')?.parentElement
    expect(row?.className).toContain('sticky')
  })
})

describe('Instance settings load failure (WS-1)', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows an error with retry instead of loading forever', async () => {
    const get = vi
      .spyOn(serviceSettingsApi, 'get')
      .mockRejectedValueOnce(new Error('Service unavailable'))
      .mockResolvedValue(SETTINGS)
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={ownerAuthValue()}>
          <ServiceSettingsSection section="ai" />
        </AuthContext.Provider>
      </QueryClientProvider>,
    )

    expect(await screen.findByText("Couldn't load platform settings")).toBeInTheDocument()
    expect(screen.getByText('Service unavailable')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByLabelText('Ask prompt')).toBeInTheDocument()
    expect(get).toHaveBeenCalledTimes(2)
  })
})

describe('Instance settings lock-out confirmation (WS-24)', () => {
  it('confirms a session cookie rename before saving it', async () => {
    const update = renderSection('security')

    fireEvent.change(await screen.findByLabelText('Session cookie'), {
      target: { value: 'tripl_sid' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(/signs everyone out/)
    expect(update).not.toHaveBeenCalled()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Save anyway' }))
    await waitFor(() =>
      expect(update).toHaveBeenCalledWith({ security: { session_cookie_name: 'tripl_sid' } }),
    )
  })

  it('saves a harmless security change without asking', async () => {
    const update = renderSection('security')

    fireEvent.change(await screen.findByLabelText('Login limit'), { target: { value: '9' } })
    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith({ security: { rate_limit_login_per_minute: 9 } }),
    )
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })
})

describe('Instance settings numeric fields (WS-25)', () => {
  it('keeps an emptied number empty and blocks Save until it is valid', async () => {
    renderSection('ai')

    const timeout = await screen.findByLabelText('Timeout seconds')
    fireEvent.change(timeout, { target: { value: '' } })

    expect(timeout).toHaveValue(null)
    expect(timeout).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByRole('button', { name: /Save changes/ })).toBeDisabled()

    fireEvent.change(timeout, { target: { value: '45' } })
    expect(screen.getByRole('button', { name: /Save changes/ })).toBeEnabled()
  })
})

describe('Instance settings AI availability (WS-29)', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('refreshes the cached AI status after an AI save', async () => {
    const invalidate = vi.spyOn(QueryClient.prototype, 'invalidateQueries')
    const update = renderSection('ai')

    fireEvent.change(await screen.findByLabelText('Ask prompt'), { target: { value: 'Be brief.' } })
    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))

    await waitFor(() => expect(update).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['aiStatus'] }))
  })
})

describe('Instance storage backend cards (WS-30)', () => {
  it('marks the card of the backend that is not selected as inactive', async () => {
    renderSection('storage')

    expect(
      await screen.findByText(/Inactive — the backend above is Local filesystem/),
    ).toBeInTheDocument()
    expect(screen.queryByText(/Inactive — the backend above is Google Cloud Storage/)).toBeNull()
  })
})

describe('Instance settings review 208 follow-ups', () => {
  it('keeps a failed save error on the section it belongs to', async () => {
    vi.spyOn(serviceSettingsApi, 'get').mockResolvedValue(SETTINGS)
    vi.spyOn(serviceSettingsApi, 'update').mockRejectedValue(new Error('Invalid CORS origin'))
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const tree = (current: 'ai' | 'security') => (
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={ownerAuthValue()}>
          <ServiceSettingsSection section={current} />
        </AuthContext.Provider>
      </QueryClientProvider>
    )
    const view = render(tree('security'))

    fireEvent.change(await screen.findByLabelText('Login limit'), { target: { value: '9' } })
    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid CORS origin')

    view.rerender(tree('ai'))
    await screen.findByLabelText('Ask prompt')
    expect(screen.queryByText('Invalid CORS origin')).toBeNull()

    view.rerender(tree('security'))
    expect(await screen.findByText('Invalid CORS origin')).toBeInTheDocument()
  })

  it('does not block Save on an out-of-range value the environment delivered', async () => {
    vi.spyOn(serviceSettingsApi, 'get').mockResolvedValue({
      ...SETTINGS,
      ai: { ...SETTINGS.ai, ai_timeout_seconds: 0 },
    })
    vi.spyOn(serviceSettingsApi, 'update').mockResolvedValue(SETTINGS)
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={ownerAuthValue()}>
          <ServiceSettingsSection section="ai" />
        </AuthContext.Provider>
      </QueryClientProvider>,
    )

    fireEvent.change(await screen.findByLabelText('Ask prompt'), { target: { value: 'Be brief.' } })

    expect(screen.getByLabelText('Timeout seconds')).not.toHaveAttribute('aria-invalid')
    expect(screen.getByRole('button', { name: /Save changes/ })).toBeEnabled()
  })
})
