import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { orgSettingsApi, type OrgSettings } from '@/api/orgSettings'
import { ActiveOrgContext } from '@/components/active-org-context'
import { orgSettingsFixture } from '@/test/orgSettings'
import OrgSettingsSection from './OrgSettingsSection'
import type { OrgSection } from './org-settings/orgSettingsModel'

/**
 * Organization › Email, AI and Limits (F20 PR9): the organization's own values
 * against `/orgs/{org}/settings`, each badged with its source, with the
 * operator's ceilings and the endpoint/credential group said out loud.
 */

function renderSection(section: OrgSection, settings: OrgSettings = orgSettingsFixture()) {
  const get = vi.spyOn(orgSettingsApi, 'get').mockResolvedValue(settings)
  const update = vi.spyOn(orgSettingsApi, 'update').mockResolvedValue(settings)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <ActiveOrgContext.Provider value={{ slug: 'acme', membership: null, orgs: [] }}>
        <MemoryRouter>
          <OrgSettingsSection section={section} />
        </MemoryRouter>
      </ActiveOrgContext.Provider>
    </QueryClientProvider>,
  )
  return { get, update }
}

function input(label: string): HTMLElement {
  return screen.getByLabelText(label)
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Organization › Limits', () => {
  it("reads the active organization's settings and badges each value's source", async () => {
    const { get } = renderSection('limits')

    expect(await screen.findByDisplayValue('20000')).toBeInTheDocument()
    expect(get).toHaveBeenCalledWith('acme')
    expect(screen.getByText('Organization', { selector: '[title]' })).toBeInTheDocument()
    expect(screen.getByText('Operator', { selector: '[title]' })).toBeInTheDocument()
    // What clearing the organization's own value would give back.
    expect(screen.getByText(/Without it: 50,000\./)).toBeInTheDocument()
    expect(screen.getByText('Operator maximum: 50,000 rows.')).toBeInTheDocument()
  })

  it('saves only the edited limit, as a number, for this organization', async () => {
    const { update } = renderSection('limits')
    await screen.findByDisplayValue('20000')

    fireEvent.change(input('Scan row limit default'), { target: { value: '1000' } })
    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith('acme', { limits: { scan_row_limit_default: 1000 } }),
    )
  })

  it("blocks Save above the operator's maximum (critique #15)", async () => {
    const { update } = renderSection('limits')
    await screen.findByDisplayValue('20000')

    fireEvent.change(input('Metrics row limit default'), { target: { value: '200000' } })

    expect(screen.getByText("The operator's maximum is 100,000.")).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Save changes/ })).toBeDisabled()
    expect(update).not.toHaveBeenCalled()
  })

  it("clears the organization's value to inherit the operator's again", async () => {
    const { update } = renderSection('limits')
    await screen.findByDisplayValue('20000')

    fireEvent.click(screen.getByRole('button', { name: 'Use the inherited value' }))

    expect(input('Scan row limit default')).toHaveValue(50000)
    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))
    await waitFor(() =>
      expect(update).toHaveBeenCalledWith('acme', { limits: { scan_row_limit_default: null } }),
    )
  })
})

describe('Organization › AI', () => {
  it("warns that the operator's key does not follow an endpoint set here (critique #13)", async () => {
    renderSection('ai')
    await screen.findByDisplayValue('https://api.operator.example/v1')

    fireEvent.change(input('Base URL'), { target: { value: 'https://llm.acme.example/v1' } })

    const note = screen.getByText(/the operator’s key is never sent to an endpoint set here/)
    expect(note).toHaveTextContent(/take the built-in defaults, not the operator’s values/)

    fireEvent.change(input('API key'), { target: { value: 'sk-acme' } })
    expect(screen.queryByText(/the operator’s key is never sent to an endpoint set here/)).toBeNull()
  })

  it('says when the operator policy leaves the organization without AI', async () => {
    const settings = orgSettingsFixture({
      operator_fallback: 'none',
      sources: { ...orgSettingsFixture().sources, 'ai.ai_base_url': 'disabled', 'ai.ai_api_key': 'disabled' },
    })
    renderSection('ai', settings)

    expect(await screen.findByText(/its AI is off/)).toBeInTheDocument()
    expect(screen.getAllByText('Disabled by operator policy')).toHaveLength(2)
  })

  it("probes the organization's own provider", async () => {
    const testAi = vi.spyOn(orgSettingsApi, 'testAi').mockResolvedValue({ ok: true, message: 'Reply received' })
    renderSection('ai')
    await screen.findByDisplayValue('gpt-operator')

    fireEvent.click(screen.getByRole('button', { name: /Test AI/ }))

    expect(await screen.findByText('Reply received')).toBeInTheDocument()
    expect(testAi).toHaveBeenCalledWith('acme')
  })
})

describe('Organization › Email', () => {
  it('says account mail stays on the platform relay, and tests this organization’s', async () => {
    const testEmail = vi
      .spyOn(orgSettingsApi, 'testEmail')
      .mockResolvedValue({ ok: true, message: 'Sent to owner@example.com' })
    renderSection('email')
    await screen.findByDisplayValue('smtp.operator.example')

    expect(screen.getByText(/password-reset and invitation mail always go through the platform/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Send test email/ }))

    expect(await screen.findByText('Sent to owner@example.com')).toBeInTheDocument()
    expect(testEmail).toHaveBeenCalledWith('acme')
  })

  it("goes back to the operator's relay as one group", async () => {
    const base = orgSettingsFixture()
    const settings = orgSettingsFixture({
      email: { ...base.email, smtp_host: 'smtp.acme.example', smtp_password_configured: false },
      sources: { ...base.sources, 'email.smtp_host': 'org', 'email.smtp_password': 'default' },
    })
    const { update } = renderSection('email', settings)
    await screen.findByDisplayValue('smtp.acme.example')

    fireEvent.click(screen.getByRole('button', { name: "Use the operator's relay" }))
    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))

    await waitFor(() => expect(update).toHaveBeenCalledWith('acme', { email: { smtp_host: null } }))
  })

  it('names a self-hosted default organization as the operator scope, with no ceilings', async () => {
    renderSection('limits', orgSettingsFixture({ scope: 'operator' }))
    const note = await screen.findByText(/these are the platform’s own settings/)

    expect(within(note).getByText(/account mail/, { exact: false })).toBeInTheDocument()
    expect(screen.queryByText(/Operator maximum/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Use the inherited value' })).toBeNull()
  })
})
