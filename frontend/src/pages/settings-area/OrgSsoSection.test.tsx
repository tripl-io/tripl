import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import { SAML_NAME_ID_EMAIL, ssoApi, type SsoConfig, type SsoDomain } from '@/api/sso'
import { ActiveOrgContext } from '@/components/active-org-context'
import OrgSsoSection from './OrgSsoSection'

/**
 * Organization › Single sign-on (F20): the provider (secret write-only), the
 * domains proved by DNS TXT, and turning SSO on and requiring it.
 */

function config(overrides: Partial<SsoConfig> = {}): SsoConfig {
  return {
    issuer: 'https://idp.example.com',
    client_id: 'tripl',
    client_secret_configured: true,
    scopes: 'openid email profile',
    enabled: false,
    sso_required: false,
    domains: [],
    ...overrides,
  }
}

const pendingDomain: SsoDomain = {
  id: 'd1',
  domain: 'example.com',
  verification_token: 'tok-123',
  verified_at: null,
}
const verifiedDomain: SsoDomain = { ...pendingDomain, verified_at: '2026-09-01T10:00:00Z' }

function renderSection(saved: SsoConfig = config(), domains: SsoDomain[] = [pendingDomain]) {
  const get = vi.spyOn(ssoApi, 'get').mockResolvedValue(saved)
  const list = vi.spyOn(ssoApi, 'listDomains').mockResolvedValue(domains)
  const update = vi.spyOn(ssoApi, 'update').mockResolvedValue(saved)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <ActiveOrgContext.Provider value={{ slug: 'acme', membership: null, orgs: [] }}>
        <MemoryRouter>
          <OrgSsoSection />
        </MemoryRouter>
      </ActiveOrgContext.Provider>
    </QueryClientProvider>,
  )
  return { get, list, update }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Organization › Single sign-on', () => {
  it('reads the provider with the secret write-only and the redirect URI to register', async () => {
    const { get } = renderSection()

    expect(await screen.findByDisplayValue('https://idp.example.com')).toBeInTheDocument()
    expect(get).toHaveBeenCalledWith('acme')
    expect(screen.getByLabelText('Client secret')).toHaveValue('')
    expect(screen.getByLabelText('Client secret')).toHaveAttribute(
      'placeholder',
      'Configured — leave blank to keep',
    )
    expect(screen.getByText(/\/api\/v1\/auth\/sso\/acme\/callback$/)).toBeInTheDocument()
  })

  it('saves the whole configuration, with a secret only when typed', async () => {
    const { update } = renderSection()
    await screen.findByDisplayValue('tripl')

    fireEvent.change(screen.getByLabelText('Client ID'), { target: { value: 'tripl-web' } })
    fireEvent.change(screen.getByLabelText('Client secret'), { target: { value: 'n3w-secret' } })
    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith('acme', {
        protocol: 'oidc',
        issuer: 'https://idp.example.com',
        client_id: 'tripl-web',
        client_secret: 'n3w-secret',
        scopes: 'openid email profile',
        saml_idp_entity_id: null,
        saml_idp_sso_url: null,
        saml_idp_certs: null,
        saml_name_id_format: SAML_NAME_ID_EMAIL,
        saml_email_attribute: null,
        enabled: false,
        sso_required: false,
      }),
    )
  })

  it('refuses an issuer that is not https before saving', async () => {
    const { update } = renderSection()
    await screen.findByDisplayValue('https://idp.example.com')

    fireEvent.change(screen.getByLabelText('Issuer URL'), { target: { value: 'http://idp.example.com' } })

    expect(screen.getByText('The issuer must use https.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Save changes/ })).toBeDisabled()
    expect(update).not.toHaveBeenCalled()
  })

  it("uses the server's spelling of the TXT record when it sends one", async () => {
    renderSection(config(), [
      {
        id: 'd3',
        domain: 'example.net',
        verified_at: null,
        verified: false,
        txt_record_name: '_tripl-verification.example.net',
        txt_record_value: 'tripl-verification=srv-token',
      },
    ])

    expect(await screen.findByText('_tripl-verification.example.net')).toBeInTheDocument()
    expect(screen.getByText('tripl-verification=srv-token')).toBeInTheDocument()
  })

  it('asks for the issuer and client ID before the first save', async () => {
    const { update } = renderSection(config({ configured: false, issuer: '', client_id: '', client_secret_configured: false }), [])
    await screen.findByLabelText('Issuer URL')

    fireEvent.change(screen.getByLabelText('Client secret'), { target: { value: 's3cret' } })

    expect(screen.getByText('Enter the issuer URL and client ID to save.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Save changes/ })).toBeDisabled()
    expect(update).not.toHaveBeenCalled()
  })

  it('shows the TXT record to publish and verifies the domain', async () => {
    renderSection()
    const verify = vi.spyOn(ssoApi, 'verifyDomain').mockResolvedValue(verifiedDomain)

    expect(await screen.findByText('_tripl-verification.example.com')).toBeInTheDocument()
    expect(screen.getByText('tripl-verification=tok-123')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Verify' }))

    await waitFor(() => expect(verify).toHaveBeenCalledWith('acme', 'd1'))
    expect(await screen.findByText('Verified.')).toBeInTheDocument()
  })

  it("says so when the TXT record is not there yet", async () => {
    renderSection()
    vi.spyOn(ssoApi, 'verifyDomain').mockResolvedValue(pendingDomain)

    fireEvent.click(await screen.findByRole('button', { name: 'Verify' }))

    expect(await screen.findByText(/TXT record was not found yet/)).toBeInTheDocument()
  })

  it('adds a domain, lowercased', async () => {
    renderSection()
    const add = vi.spyOn(ssoApi, 'addDomain').mockResolvedValue({ ...pendingDomain, id: 'd2', domain: 'example.org' })
    await screen.findByText('_tripl-verification.example.com')

    fireEvent.change(screen.getByLabelText('Domain to add'), { target: { value: 'Example.ORG' } })
    fireEvent.click(screen.getByRole('button', { name: /Add domain/ }))

    await waitFor(() => expect(add).toHaveBeenCalledWith('acme', 'example.org'))
  })

  it('shows a domain another organization holds as the server says', async () => {
    renderSection()
    vi.spyOn(ssoApi, 'addDomain').mockRejectedValue(
      new ApiError('This domain is already verified by another organization', 409),
    )
    await screen.findByText('_tripl-verification.example.com')

    fireEvent.change(screen.getByLabelText('Domain to add'), { target: { value: 'example.net' } })
    fireEvent.click(screen.getByRole('button', { name: /Add domain/ }))

    expect(await screen.findByText(/already verified by another organization/)).toBeInTheDocument()
  })

  it('keeps SSO off until a domain is verified', async () => {
    renderSection()
    await screen.findByDisplayValue('https://idp.example.com')

    expect(screen.getByRole('switch', { name: 'Enable single sign-on' })).toBeDisabled()
    expect(screen.getByText('Verify at least one email domain first.')).toBeInTheDocument()
  })

  it('turns SSO on once a domain is verified', async () => {
    const { update } = renderSection(config(), [verifiedDomain])
    await screen.findByDisplayValue('https://idp.example.com')

    fireEvent.click(screen.getByRole('switch', { name: 'Enable single sign-on' }))

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith('acme', expect.objectContaining({ enabled: true, sso_required: false })),
    )
    expect(update.mock.calls[0]?.[1]).not.toHaveProperty('client_secret')
  })

  it('requires SSO only after a confirm that names key revocation and the owner break-glass', async () => {
    const { update } = renderSection(config({ enabled: true }), [verifiedDomain])
    update.mockResolvedValue({ ...config({ enabled: true, sso_required: true }), revoked_api_keys: 3 })
    await screen.findByDisplayValue('https://idp.example.com')

    fireEvent.click(screen.getByRole('switch', { name: 'Require single sign-on' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).getByText(/API keys are revoked/)).toBeInTheDocument()
    expect(within(dialog).getByText(/break-glass/)).toBeInTheDocument()
    expect(within(dialog).getByText(/keys included/)).toBeInTheDocument()
    expect(update).not.toHaveBeenCalled()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Require single sign-on' }))

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith('acme', expect.objectContaining({ enabled: true, sso_required: true })),
    )
    expect(await screen.findByText(/3 API keys were revoked/)).toBeInTheDocument()
  })
})

const CERT = '-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIUQ2VydA==\n-----END CERTIFICATE-----'

function samlConfig(overrides: Partial<SsoConfig> = {}): SsoConfig {
  return config({
    protocol: 'saml',
    issuer: null,
    client_id: null,
    client_secret_configured: false,
    scopes: null,
    saml_idp_entity_id: 'https://idp.example.com/saml',
    saml_idp_sso_url: 'https://idp.example.com/sso/saml',
    saml_idp_certs: CERT,
    saml_name_id_format: SAML_NAME_ID_EMAIL,
    saml_email_attribute: null,
    saml_sp_entity_id: 'https://tripl.example.com/api/v1/auth/sso/acme/saml/metadata',
    saml_acs_url: 'https://tripl.example.com/api/v1/auth/sso/acme/saml/acs',
    saml_metadata_url: 'https://tripl.example.com/api/v1/auth/sso/acme/saml/metadata',
    saml_cert_info: [
      { fingerprint_sha256: 'ab01cd', not_after: '2099-01-01T00:00:00Z', subject: 'CN=idp.example.com' },
      { fingerprint_sha256: 'ef02ab', not_after: '2000-01-01T00:00:00Z', subject: 'CN=old.example.com' },
    ],
    ...overrides,
  })
}

describe('Organization › Single sign-on › SAML 2.0', () => {
  it('shows what to register at the IdP and the saved certificates with their expiry', async () => {
    renderSection(samlConfig())

    expect(await screen.findByDisplayValue('https://idp.example.com/saml')).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /SAML 2\.0/ })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getAllByText('https://tripl.example.com/api/v1/auth/sso/acme/saml/metadata')).toHaveLength(2)
    expect(screen.getByText('https://tripl.example.com/api/v1/auth/sso/acme/saml/acs')).toBeInTheDocument()
    expect(screen.getByText('SHA-256 AB:01:CD')).toBeInTheDocument()
    expect(screen.getByText('CN=old.example.com')).toBeInTheDocument()
    expect(screen.getByText(/^Expired/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Client secret')).not.toBeInTheDocument()
  })

  it('switches the protocol and saves the SAML fields, keeping the OpenID Connect ones', async () => {
    const { update } = renderSection()
    await screen.findByDisplayValue('https://idp.example.com')

    fireEvent.click(screen.getByRole('radio', { name: /SAML 2\.0/ }))
    expect(screen.getByText('Enter the IdP entity ID, SSO URL and signing certificate to save.')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('IdP entity ID'), { target: { value: 'https://idp.example.com/saml' } })
    fireEvent.change(screen.getByLabelText('SSO URL'), { target: { value: 'https://idp.example.com/sso/saml' } })
    fireEvent.change(screen.getByLabelText('Signing certificates'), { target: { value: CERT } })
    fireEvent.change(screen.getByLabelText('Email attribute'), { target: { value: 'email' } })
    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith('acme', {
        protocol: 'saml',
        issuer: 'https://idp.example.com',
        client_id: 'tripl',
        scopes: 'openid email profile',
        saml_idp_entity_id: 'https://idp.example.com/saml',
        saml_idp_sso_url: 'https://idp.example.com/sso/saml',
        saml_idp_certs: CERT,
        saml_name_id_format: SAML_NAME_ID_EMAIL,
        saml_email_attribute: 'email',
        enabled: false,
        sso_required: false,
      }),
    )
  })

  it('refuses an SSO URL that is not https and a certificate that is not PEM', async () => {
    const { update } = renderSection(samlConfig())
    await screen.findByDisplayValue('https://idp.example.com/saml')

    fireEvent.change(screen.getByLabelText('SSO URL'), { target: { value: 'http://idp.example.com/sso' } })
    fireEvent.change(screen.getByLabelText('Signing certificates'), { target: { value: 'MIIBszCCAVmg' } })

    expect(screen.getByText('The SSO URL must use https.')).toBeInTheDocument()
    expect(screen.getByText(/Paste the certificate in PEM form/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Save changes/ })).toBeDisabled()
    expect(update).not.toHaveBeenCalled()
  })

  it('fills the IdP values from pasted metadata without saving them', async () => {
    const { update } = renderSection(config({ configured: false, issuer: '', client_id: '', client_secret_configured: false }), [])
    const imported = vi.spyOn(ssoApi, 'importSamlMetadata').mockResolvedValue({
      saml_idp_entity_id: 'https://idp.example.com/meta',
      saml_idp_sso_url: 'https://idp.example.com/sso/redirect',
      saml_idp_certs: CERT,
    })
    await screen.findByLabelText('Issuer URL')

    fireEvent.click(screen.getByRole('radio', { name: /SAML 2\.0/ }))
    fireEvent.change(screen.getByLabelText('Metadata XML'), {
      target: { value: '<EntityDescriptor entityID="https://idp.example.com/meta"/>' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Import' }))

    await waitFor(() =>
      expect(imported).toHaveBeenCalledWith('acme', '<EntityDescriptor entityID="https://idp.example.com/meta"/>'),
    )
    expect(await screen.findByDisplayValue('https://idp.example.com/meta')).toBeInTheDocument()
    expect(screen.getByDisplayValue('https://idp.example.com/sso/redirect')).toBeInTheDocument()
    expect(screen.getByText(/Review the values below, then save/)).toBeInTheDocument()
    expect(update).not.toHaveBeenCalled()
  })

  it('says why metadata could not be read', async () => {
    renderSection(samlConfig())
    vi.spyOn(ssoApi, 'importSamlMetadata').mockRejectedValue(
      new ApiError('The metadata has no HTTP-Redirect single sign-on location', 422),
    )
    await screen.findByDisplayValue('https://idp.example.com/saml')

    fireEvent.change(screen.getByLabelText('Metadata XML'), { target: { value: '<EntityDescriptor/>' } })
    fireEvent.click(screen.getByRole('button', { name: 'Import' }))

    expect(await screen.findByText(/no HTTP-Redirect single sign-on location/)).toBeInTheDocument()
  })

  it('checks the saved SAML settings', async () => {
    renderSection(samlConfig())
    const test = vi.spyOn(ssoApi, 'test').mockResolvedValue({ ok: true, message: 'The certificates are valid.' })

    fireEvent.click(await screen.findByRole('button', { name: 'Check settings' }))

    await waitFor(() => expect(test).toHaveBeenCalledWith('acme'))
    expect(await screen.findByText('The certificates are valid.')).toBeInTheDocument()
  })

  it('turns SAML single sign-on on without a client secret', async () => {
    const { update } = renderSection(samlConfig(), [verifiedDomain])
    await screen.findByDisplayValue('https://idp.example.com/saml')

    fireEvent.click(screen.getByRole('switch', { name: 'Enable single sign-on' }))

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith('acme', expect.objectContaining({ protocol: 'saml', enabled: true })),
    )
  })
})
