import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import { auditWebhookApi, type AuditWebhook, type AuditWebhookDelivery } from '@/api/auditExport'
import { ActiveOrgContext } from '@/components/active-org-context'
import OrgAuditWebhookSection from './OrgAuditWebhookSection'

/**
 * Organization › Audit webhook (F20): the endpoint, the signing secret shown
 * once, a test delivery, and the recent deliveries.
 */

function webhook(overrides: Partial<AuditWebhook> = {}): AuditWebhook {
  return {
    configured: true,
    url: 'https://siem.example.com/hooks/tripl',
    enabled: true,
    secret_configured: true,
    last_success_at: null,
    last_error: null,
    last_error_at: null,
    ...overrides,
  }
}

const deadDelivery: AuditWebhookDelivery = {
  id: 'o1',
  audit_log_id: 'a1',
  action: 'org.member_role_changed',
  status: 'dead',
  attempts: 8,
  next_attempt_at: null,
  last_error: 'HTTP 500',
  created_at: '2026-09-27T10:00:00Z',
  sent_at: null,
}

function mountSection() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <ActiveOrgContext.Provider value={{ slug: 'acme', membership: null, orgs: [] }}>
        <MemoryRouter>
          <OrgAuditWebhookSection />
        </MemoryRouter>
      </ActiveOrgContext.Provider>
    </QueryClientProvider>,
  )
}

function renderSection(saved: AuditWebhook | null = webhook(), deliveries: AuditWebhookDelivery[] = []) {
  const get = vi.spyOn(auditWebhookApi, 'get').mockResolvedValue(saved)
  const list = vi.spyOn(auditWebhookApi, 'deliveries').mockResolvedValue(deliveries)
  mountSection()
  return { get, list }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Organization › Audit webhook', () => {
  it('reads the server’s 200 {configured: false} as no webhook: create, on by default, no actions', async () => {
    // The real client, not a mocked `get`: the server answers 200 when there is none.
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) =>
      Promise.resolve(
        new Response(
          JSON.stringify(
            (init?.method ?? 'GET').toUpperCase() === 'PUT'
              ? { ...webhook(), secret: 'whsec_synthetic_789' }
              : String(input instanceof Request ? input.url : input).includes('/deliveries')
                ? []
                : {
                  configured: false,
                  url: '',
                  enabled: false,
                  secret_configured: false,
                  last_success_at: null,
                  last_error: null,
                  last_error_at: null,
                },
          ),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )
    mountSection()

    const url = await screen.findByLabelText('URL')
    expect(url).toHaveValue('')
    expect(screen.getByText('New audit entries are queued for delivery.')).toBeInTheDocument()
    for (const name of ['Send test event', 'Rotate secret', 'Delete webhook']) {
      expect(screen.queryByRole('button', { name })).toBeNull()
    }
    expect(screen.queryByText('Last successful delivery')).toBeNull()

    fireEvent.change(url, { target: { value: 'https://siem.example.com/hooks/tripl' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create webhook' }))

    await waitFor(() =>
      expect(
        fetchSpy.mock.calls.some(
          ([, init]) =>
            (init?.method ?? '').toUpperCase() === 'PUT'
            && init?.body === JSON.stringify({ url: 'https://siem.example.com/hooks/tripl', enabled: true }),
        ),
      ).toBe(true),
    )
  })

  it('creates the webhook and shows the signing secret once', async () => {
    const save = vi
      .spyOn(auditWebhookApi, 'save')
      .mockResolvedValue({ ...webhook(), secret: 'whsec_synthetic_123' })
    renderSection(null)

    const url = await screen.findByLabelText('URL')
    expect(screen.queryByRole('button', { name: 'Send test event' })).toBeNull()
    fireEvent.change(url, { target: { value: 'https://siem.example.com/hooks/tripl' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create webhook' }))

    await waitFor(() =>
      expect(save).toHaveBeenCalledWith('acme', { url: 'https://siem.example.com/hooks/tripl', enabled: true }),
    )
    expect(await screen.findByLabelText('Signing secret')).toHaveValue('whsec_synthetic_123')

    fireEvent.click(screen.getByRole('button', { name: 'I’ve saved it' }))
    expect(screen.queryByLabelText('Signing secret')).toBeNull()
    // Gone for good: the status card says a secret exists, not what it is.
    expect(screen.getByText('Configured')).toBeInTheDocument()
    expect(screen.queryByText('whsec_synthetic_123')).toBeNull()
  })

  it('will not save a plain-http URL', async () => {
    const save = vi.spyOn(auditWebhookApi, 'save')
    renderSection(null)

    fireEvent.change(await screen.findByLabelText('URL'), { target: { value: 'http://siem.example.com' } })

    expect(screen.getByRole('button', { name: 'Create webhook' })).toBeDisabled()
    expect(screen.getAllByText(/must start with https:\/\//).length).toBeGreaterThan(0)
    expect(save).not.toHaveBeenCalled()
  })

  it('shows the server refusing a private address', async () => {
    vi.spyOn(auditWebhookApi, 'save').mockRejectedValue(
      new ApiError('The URL must resolve to a public address', 422),
    )
    renderSection(null)

    fireEvent.change(await screen.findByLabelText('URL'), { target: { value: 'https://10.0.0.5/hook' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create webhook' }))

    expect(await screen.findByText(/resolve to a public address/)).toBeInTheDocument()
  })

  it('rotates the secret after a confirm and shows the new one once', async () => {
    const rotate = vi.spyOn(auditWebhookApi, 'rotateSecret').mockResolvedValue({ ...webhook(), secret: 'whsec_rotated_456' })
    renderSection()

    fireEvent.click(await screen.findByRole('button', { name: 'Rotate secret' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Rotate secret' }))

    await waitFor(() => expect(rotate).toHaveBeenCalledWith('acme'))
    expect(await screen.findByLabelText('Signing secret')).toHaveValue('whsec_rotated_456')
  })

  it('sends a test event and reports what the receiver answered', async () => {
    const test = vi
      .spyOn(auditWebhookApi, 'test')
      .mockResolvedValue({ ok: false, status_code: 401, error: 'Signature rejected' })
    renderSection()

    fireEvent.click(await screen.findByRole('button', { name: 'Send test event' }))

    await waitFor(() => expect(test).toHaveBeenCalledWith('acme'))
    expect(await screen.findByText('Not delivered (HTTP 401): Signature rejected')).toBeInTheDocument()
  })

  it('lists recent deliveries and filters them by status', async () => {
    const { list } = renderSection(webhook(), [deadDelivery])

    expect(await screen.findByText('org.member_role_changed')).toBeInTheDocument()
    expect(screen.getByText('Gave up', { selector: '[data-slot="badge"]' })).toBeInTheDocument()
    expect(list).toHaveBeenCalledWith('acme', { status: undefined, limit: 50 })

    fireEvent.change(screen.getByLabelText('Delivery status'), { target: { value: 'dead' } })

    await waitFor(() => expect(list).toHaveBeenCalledWith('acme', { status: 'dead', limit: 50 }))
  })

  it('deletes the webhook after a confirm', async () => {
    const remove = vi.spyOn(auditWebhookApi, 'remove').mockResolvedValue(undefined)
    renderSection()

    fireEvent.click(await screen.findByRole('button', { name: 'Delete webhook' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete webhook' }))

    await waitFor(() => expect(remove).toHaveBeenCalledWith('acme'))
    expect(await screen.findByRole('button', { name: 'Create webhook' })).toBeInTheDocument()
  })
})
