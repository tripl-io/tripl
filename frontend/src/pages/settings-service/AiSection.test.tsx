import { fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { serviceSettingsApi } from '@/api/serviceSettings'
import type { ServiceSettings } from '@/types'
import { AiSection } from './AiSection'
import {
  COMPARE_FIELDS,
  EMPTY_SECRET_DRAFTS,
  buildUpdate,
  editableFromSettings,
  numberFieldError,
} from './serviceSettingsHelpers'

const AI: ServiceSettings['ai'] = {
  ai_enabled: false,
  ai_base_url: 'https://api.openai.com/v1',
  ai_model: 'gpt-4o-mini',
  ai_api_key_configured: false,
  ai_timeout_seconds: 30,
  ai_max_output_tokens: 700,
  describe_system_prompt: 'describe',
  ask_system_prompt: 'ask',
  alert_explanation_system_prompt: 'explain',
  search_embeddings_enabled: true,
  search_embedding_provider: 'openai',
  search_embedding_model: 'text-embedding-3-small',
  search_embedding_api_key_configured: false,
  search_embedding_dimensions: 1536,
  search_embedding_base_url: 'https://api.openai.com/v1',
}

function settingsFixture(
  ai: Partial<ServiceSettings['ai']> = {},
  sources: ServiceSettings['sources'] = {},
): ServiceSettings {
  return {
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
      smtp_password_configured: false,
      smtp_security: 'starttls',
      smtp_from_address: '',
    },
    ai: { ...AI, ...ai },
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
    sources,
  } as ServiceSettings
}

function renderSection(
  settings: ServiceSettings,
  form = editableFromSettings(settings),
  setField = vi.fn(),
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AiSection
        form={form}
        settings={settings}
        secretDrafts={EMPTY_SECRET_DRAFTS}
        setField={setField}
        setSecretDrafts={vi.fn()}
        saving={false}
        onClearSecret={vi.fn()}
        platformAdmin
      />
    </QueryClientProvider>,
  )
}

/**
 * The label + badge cluster of one row, so each row's badge can be read on its
 * own — every other field in the section carries a badge too.
 */
function labelRow(label: string): HTMLElement {
  // The kit Field wraps the label (and its required mark) in its own span; the
  // row that also holds `labelRight` — the source badge — is the div around it.
  const row = screen.getByText(label).closest('div')
  if (!row) throw new Error(`no label row around "${label}"`)
  return row
}

/**
 * SEARCH_EMBEDDING_BASE_URL decides where every indexed event name, description
 * and field value is POSTed, and it has been dropped from the compose env
 * allowlist three times. Nothing in the running system reported it, so the only
 * way to notice was to read the source and diff values by hand.
 */
describe('Instance AI — the embeddings endpoint', () => {
  it('shows the endpoint the indexed plan text is actually sent to', () => {
    renderSection(settingsFixture({ search_embedding_base_url: 'https://llm.internal/v1' }))

    expect(
      within(screen.getByRole('group', { name: 'Embeddings base URL' })).getByText(
        'https://llm.internal/v1',
      ),
    ).toBeInTheDocument()
  })

  it('never lets it be edited, because repointing it poisons the existing index', () => {
    renderSection(settingsFixture())

    // Reported as text, not a dashed input that cannot move (ST-30).
    const row = screen.getByRole('group', { name: 'Embeddings base URL' })
    expect(within(row).queryByRole('textbox')).toBeNull()
  })

  it('says which variable sets it and what changing it really costs', () => {
    renderSection(settingsFixture())

    const row = screen.getByRole('group', { name: 'Embeddings base URL' })
    expect(within(row).getByText('SEARCH_EMBEDDING_BASE_URL')).toBeInTheDocument()
    // The reasoning sits behind a "Why?" rather than eight lines of hint (ST-30).
    const why = within(row).getByText('Why?').closest('details')
    expect(why).toHaveTextContent(/POSTed here/i)
    expect(why).toHaveTextContent(/re-embed and a deploy, not a setting/i)
  })

  it('does not assert an env variable for a value at its built-in default', () => {
    // The prod state the issue documents: the value is bit-identical to the
    // shipped default and nothing was ever delivered for it. No badge at all
    // (ST-25); the page legend says what an unmarked row means.
    renderSection(settingsFixture({}, { 'ai.search_embedding_base_url': 'default' }))

    expect(within(labelRow('Embeddings base URL')).queryByText('Env')).toBeNull()
    expect(within(labelRow('Embeddings base URL')).queryByText('Default')).toBeNull()
  })

  it('badges a delivered endpoint as Env, which is the evidence the issue asked for', () => {
    renderSection(
      settingsFixture(
        { search_embedding_base_url: 'https://llm.internal/v1' },
        { 'ai.search_embedding_base_url': 'env' },
      ),
    )

    expect(within(labelRow('Embeddings base URL')).getByText('Env')).toBeInTheDocument()
  })

  it('badges the other read-only embedding field the same way', () => {
    // Endpoint and width are the two facts that describe the vector space every
    // stored embedding was written into; a badge on one and not the other would
    // read as an oversight.
    renderSection(settingsFixture({}, { 'ai.search_embedding_dimensions': 'env' }))

    expect(within(labelRow('Embedding dimensions')).getByText('Env')).toBeInTheDocument()
  })

  it('keeps the endpoint out of the update payload entirely', () => {
    // The frontend gate: absent from COMPARE_FIELDS, so buildSectionDiff can
    // never put it in a PATCH even if something wrote it into the form. The
    // backend gates on AiSettingsUpdate and EDITABLE_FIELDS independently.
    const saved = settingsFixture()
    const base = editableFromSettings(saved)
    const form = {
      ...base,
      ai: { ...base.ai, search_embedding_base_url: 'https://evil.example/v1', ai_model: 'gpt-5' },
    }

    const update = buildUpdate(form, saved, EMPTY_SECRET_DRAFTS)

    expect(update.ai).toEqual({ ai_model: 'gpt-5' })
    expect(COMPARE_FIELDS.ai).not.toContain('search_embedding_base_url')
  })
})

/**
 * The reset card counts a stored key as an override — it is one, and Reset nulls
 * it — while the row itself rendered no badge at all. So an instance whose only
 * override was an API key showed a red "Clears the 1 AI override on this
 * instance — every field badged Override above" beside rows that all read
 * "Default": the same copy-versus-badge disagreement as before, in a
 * section that now has three badge states. overrideCount's field
 * set and the badged field set have to be one set.
 */
describe('Instance AI — the stored keys', () => {
  it('badges the keys the reset card already counts as overrides', () => {
    renderSection(
      settingsFixture({}, { 'ai.ai_api_key': 'override', 'ai.search_embedding_api_key': 'env' }),
    )

    expect(within(labelRow('AI API key')).getByText('Override')).toBeInTheDocument()
    expect(within(labelRow('Embedding API key')).getByText('Env')).toBeInTheDocument()
  })
})

describe('Instance AI — stored key deletion (WS-27)', () => {
  it('offers no delete for a key that is not stored', () => {
    renderSection(settingsFixture({ ai_api_key_configured: false }))

    const buttons = screen.getAllByRole('button', { name: 'Delete stored key' })
    expect(buttons).toHaveLength(2)
    for (const button of buttons) expect(button).toBeDisabled()
  })

  it('offers delete once a key is stored', () => {
    renderSection(settingsFixture({ ai_api_key_configured: true }))

    expect(screen.getAllByRole('button', { name: 'Delete stored key' })[0]).toBeEnabled()
  })
})

describe('Instance AI — connection test (WS-26)', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('says the test runs against the saved settings, without shouting', () => {
    renderSection(settingsFixture())

    expect(screen.getByText('Uses the saved settings, so save your changes first.')).toBeInTheDocument()
    expect(screen.queryByText(/SAVED/)).toBeNull()
  })

  it('holds the test, and says why, while AI is off in the saved settings (ST-26)', () => {
    renderSection(settingsFixture({ ai_enabled: false }))

    expect(screen.getByRole('button', { name: 'Test AI' })).toBeDisabled()
    expect(screen.getByText(/AI is off in the saved settings/)).toBeInTheDocument()
    // The dependent rows stay editable, only de-emphasised.
    expect(screen.getByText('Not used while AI is off.')).toBeInTheDocument()
    expect(screen.getByLabelText('Model')).toBeEnabled()
  })

  it('holds the test while no API key is saved (ST-26)', () => {
    renderSection(settingsFixture({ ai_enabled: true, ai_api_key_configured: false }))

    expect(screen.getByRole('button', { name: 'Test AI' })).toBeDisabled()
    expect(screen.getByText('No API key is saved. Add one and save first.')).toBeInTheDocument()
  })

  it('reports a failed test request instead of showing nothing', async () => {
    vi.spyOn(serviceSettingsApi, 'testAi').mockRejectedValue(new Error('Gateway timeout'))
    renderSection(settingsFixture({ ai_enabled: true, ai_api_key_configured: true }))

    fireEvent.click(screen.getByRole('button', { name: 'Test AI' }))

    expect(await screen.findByRole('status')).toHaveTextContent('Gateway timeout')
  })
})

describe('Instance AI — numeric fields (WS-25)', () => {
  it('passes the emptied text through instead of writing 0', () => {
    const setField = vi.fn()
    renderSection(settingsFixture(), undefined, setField)

    fireEvent.change(screen.getByLabelText('Timeout seconds'), { target: { value: '' } })

    expect(setField).toHaveBeenCalledWith('ai', 'ai_timeout_seconds', '')
  })

  it('names an empty or out-of-range value under the input', () => {
    const settings = settingsFixture()
    const base = editableFromSettings(settings)
    renderSection(settings, { ...base, ai: { ...base.ai, ai_timeout_seconds: '' } })

    const input = screen.getByLabelText('Timeout seconds')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveAccessibleDescription('Enter a whole number, 1 or more.')
  })

  it('converts valid text to a number and ignores a value typed back to the saved one', () => {
    const saved = settingsFixture()
    const base = editableFromSettings(saved)

    expect(
      buildUpdate({ ...base, ai: { ...base.ai, ai_timeout_seconds: '45' } }, saved, EMPTY_SECRET_DRAFTS),
    ).toEqual({ ai: { ai_timeout_seconds: 45 } })
    expect(
      buildUpdate({ ...base, ai: { ...base.ai, ai_timeout_seconds: '30' } }, saved, EMPTY_SECRET_DRAFTS),
    ).toEqual({})
  })

  it('checks the backend ranges', () => {
    expect(numberFieldError('ai', 'ai_timeout_seconds', '0')).not.toBeNull()
    expect(numberFieldError('ai', 'ai_timeout_seconds', '1.5')).not.toBeNull()
    expect(numberFieldError('ai', 'ai_timeout_seconds', '1')).toBeNull()
    expect(numberFieldError('email', 'smtp_port', '65536')).not.toBeNull()
    expect(numberFieldError('security', 'hsts_max_age_seconds', '0')).toBeNull()
    expect(numberFieldError('ai', 'ai_model', '')).toBeNull()
  })
})

describe('Instance AI — restore a default prompt (ST-30)', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('offers the built-in prompt beside one that differs, and fills the editor with it', async () => {
    vi.spyOn(serviceSettingsApi, 'aiPromptDefaults').mockResolvedValue({
      describe_system_prompt: 'built-in describe',
      ask_system_prompt: 'ask',
      alert_explanation_system_prompt: 'explain',
    })
    const setField = vi.fn()
    const settings = settingsFixture()
    renderSection(settings, editableFromSettings(settings), setField)

    fireEvent.click(
      await screen.findByRole('button', { name: 'Restore the default describe prompt' }),
    )
    expect(setField).toHaveBeenCalledWith('ai', 'describe_system_prompt', 'built-in describe')
    // A prompt already at its default has nothing to restore.
    expect(screen.queryByRole('button', { name: 'Restore the default ask prompt' })).toBeNull()
  })
})

describe('Instance AI — provider presets', () => {
  it('fills the base URL and model when Claude is picked', () => {
    const setField = vi.fn()
    renderSection(settingsFixture(), undefined, setField)

    fireEvent.change(screen.getByLabelText('AI provider'), { target: { value: 'anthropic' } })

    expect(setField).toHaveBeenCalledWith('ai', 'ai_base_url', 'https://api.anthropic.com/v1')
    expect(setField).toHaveBeenCalledWith('ai', 'ai_model', 'claude-haiku-4-5')
  })
})
