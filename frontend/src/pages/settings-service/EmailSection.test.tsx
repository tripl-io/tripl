import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ServiceSettings } from '@/types'
import { serviceSettingsApi } from '@/api/serviceSettings'
import { EmailSection } from './EmailSection'
import {
  COMPARE_FIELDS,
  EMPTY_SECRET_DRAFTS,
  RESET_FIELDS,
  editableFromSettings,
} from './serviceSettingsHelpers'

vi.mock('@/api/serviceSettings', () => ({
  serviceSettingsApi: { testEmail: vi.fn() },
}))

const EMAIL: ServiceSettings['email'] = {
  smtp_host: 'relay.example.com',
  smtp_port: 587,
  smtp_username: 'apikey',
  smtp_password_configured: true,
  smtp_security: 'starttls',
  smtp_from_address: 'no-reply@example.com',
}

function settingsFixture(
  email: Partial<ServiceSettings['email']> = {},
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
    email: { ...EMAIL, ...email },
    ai: {
      ai_enabled: false,
      ai_base_url: '',
      ai_model: '',
      ai_api_key_configured: false,
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
      search_embedding_base_url: '',
    },
    overridden_fields: [],
    sources,
  } as unknown as ServiceSettings
}

function renderSection(settings: ServiceSettings) {
  const setField = vi.fn()
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={queryClient}>
      <EmailSection
        form={editableFromSettings(settings)}
        settings={settings}
        secretDrafts={EMPTY_SECRET_DRAFTS}
        setField={setField}
        setSecretDrafts={vi.fn()}
        saving={false}
        onClearSecret={vi.fn()}
      />
    </QueryClientProvider>,
  )
  return { setField, ...view }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('Email settings — transport mode', () => {
  it('offers all three transports and names the port each one belongs to', () => {
    // The port is the part an operator gets wrong, and getting it wrong produces
    // no error — the send stalls until it times out. So the options
    // have to carry the ports, not just the protocol names.
    renderSection(settingsFixture())

    const select = screen.getByLabelText('Security') as HTMLSelectElement
    expect(select.value).toBe('starttls')
    expect(screen.getByRole('option', { name: /STARTTLS.*587/ })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /Implicit TLS.*465/ })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /^None/ })).toBeInTheDocument()
  })

  it('explains implicit TLS as encrypt-first rather than as more encryption', () => {
    renderSection(settingsFixture({ smtp_security: 'implicit_tls' }))

    const hint = screen.getByText(/wraps the connection in TLS before sending anything/i)
    expect(hint).toHaveTextContent(/465/)
  })

  it('warns that plaintext is only reasonable on a trusted path', () => {
    renderSection(settingsFixture({ smtp_security: 'none' }))

    expect(screen.getByText(/no encryption at all/i)).toHaveTextContent(/localhost|already trust/i)
  })

  it('routes a change through setField so it lands in the PATCH payload', () => {
    const { setField } = renderSection(settingsFixture())

    fireEvent.change(screen.getByLabelText('Security'), { target: { value: 'implicit_tls' } })

    expect(setField).toHaveBeenCalledWith('email', 'smtp_security', 'implicit_tls')
  })

  it('includes smtp_security in the email diff and reset field lists', () => {
    // Without this the control renders but never reaches PATCH /api/v1/settings —
    // the operator would pick a mode, save, and watch nothing change.
    expect(RESET_FIELDS.email).toContain('smtp_security')
    expect(COMPARE_FIELDS.email).toContain('smtp_security')
    // The replaced boolean must be gone from both, or a reset would try to clear
    // a key the API no longer accepts.
    expect(RESET_FIELDS.email).not.toContain('smtp_use_tls')
  })

  it('reports whether the mode comes from an override or the environment', () => {
    renderSection(settingsFixture({}, { 'email.smtp_security': 'override' }))

    expect(screen.getAllByText('Override')).toHaveLength(1)
  })
})

describe('Email settings — test send', () => {
  it('reports the relay failure verbatim instead of a generic error', async () => {
    // The whole point of the button: a failed password-reset send is
    // deliberately silent for the requester, so this is the only surface that can
    // say what the relay actually answered.
    vi.mocked(serviceSettingsApi.testEmail).mockResolvedValue({
      ok: false,
      message: '535 Authentication failed',
    })
    renderSection(settingsFixture())

    fireEvent.click(screen.getByRole('button', { name: /send test email/i }))

    expect(await screen.findByText('535 Authentication failed')).toBeInTheDocument()
  })

  it('confirms delivery and names the address it reached', async () => {
    vi.mocked(serviceSettingsApi.testEmail).mockResolvedValue({
      ok: true,
      message: 'Test message sent to owner@example.com.',
    })
    renderSection(settingsFixture())

    fireEvent.click(screen.getByRole('button', { name: /send test email/i }))

    await waitFor(() => {
      expect(screen.getByText(/sent to owner@example.com/i)).toBeInTheDocument()
    })
    // No recipient argument: the endpoint defaults to the signed-in owner.
    expect(serviceSettingsApi.testEmail).toHaveBeenCalledWith()
  })

  it('says the probe uses the saved settings, not the ones on screen', () => {
    // An operator who edits the port, clicks Test, and reads "sent" would
    // conclude the NEW port works. It tested the stored one.
    renderSection(settingsFixture())

    expect(screen.getByText(/Uses the saved settings, so save your changes first/)).toBeInTheDocument()
    expect(screen.queryByText(/SAVED/)).toBeNull()
  })

  it('holds the test send until an SMTP host is saved', () => {
    renderSection(settingsFixture({ smtp_host: '' }))

    expect(screen.getByRole('heading', { name: 'Send a test email' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /send test email/i })).toBeDisabled()
    expect(screen.getByText('Set an SMTP host and save first.')).toBeInTheDocument()
  })

  it('holds the test send until a From address is saved too', () => {
    renderSection(settingsFixture({ smtp_from_address: '' }))

    expect(screen.getByRole('button', { name: /send test email/i })).toBeDisabled()
    expect(screen.getByText('Set a default From address and save first.')).toBeInTheDocument()
  })
})

describe('Email settings — stored password', () => {
  it('offers to delete the stored password only when one is stored', () => {
    const { unmount } = renderSection(settingsFixture({ smtp_password_configured: true }))
    expect(screen.getByRole('button', { name: 'Delete stored password' })).toBeEnabled()
    unmount()

    // Not a greyed-out button: with nothing stored there is nothing to delete.
    renderSection(settingsFixture({ smtp_password_configured: false }))
    expect(screen.queryByRole('button', { name: 'Delete stored password' })).toBeNull()
  })
})
