import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { orgSettingsApi, type OrgSettings } from '@/api/orgSettings'
import { ActiveOrgContext } from '@/components/active-org-context'
import { orgSettingsFixture } from '@/test/orgSettings'
import OrgSettingsSection from './OrgSettingsSection'

/**
 * Organization › Storage (F20 PR11): where the organization's photos go and
 * what an upload may be. The bucket and its service-account key are one group,
 * the key is write-only, the size cap is at most the operator's and the content
 * types a subset of the operator's list.
 */

function renderStorage(settings: OrgSettings = orgSettingsFixture()) {
  vi.spyOn(orgSettingsApi, 'get').mockResolvedValue(settings)
  const update = vi.spyOn(orgSettingsApi, 'update').mockResolvedValue(settings)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <ActiveOrgContext.Provider value={{ slug: 'acme', membership: null, orgs: [] }}>
        <MemoryRouter>
          <OrgSettingsSection section="storage" />
        </MemoryRouter>
      </ActiveOrgContext.Provider>
    </QueryClientProvider>,
  )
  return { update }
}

function ownBucket(): OrgSettings {
  const base = orgSettingsFixture()
  return orgSettingsFixture({
    storage: {
      ...base.storage,
      photo_storage_backend: 'gcs',
      gcs_photo_bucket: 'acme-photos',
      gcs_photo_credentials_configured: true,
    },
    sources: {
      ...base.sources,
      'storage.photo_storage_backend': 'org',
      'storage.gcs_photo_bucket': 'org',
      'storage.gcs_photo_credentials_json': 'org',
    },
  })
}

const saveButton = () => screen.getByRole('button', { name: /Save changes/ })

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Organization › Storage', () => {
  it("shows the platform's storage and limits while the organization has none", async () => {
    renderStorage()
    expect(await screen.findByText('Where photos are stored')).toBeInTheDocument()
    expect(screen.getByText('Operator maximum: 10 MB.')).toBeInTheDocument()
    expect(
      screen.getByText(/The operator allows: image\/jpeg, image\/png, image\/gif, image\/webp\./),
    ).toBeInTheDocument()
    // Hosted: the server's disk is not the organization's to choose.
    const local = screen.getByRole('option', { name: /not on a hosted platform/ })
    expect(local).toBeDisabled()
    expect(screen.queryByRole('button', { name: /Use the platform/ })).toBeNull()
  })

  it('saves an own bucket with its key in one write, and warns until the key is there', async () => {
    const { update } = renderStorage()
    await screen.findByText('Where photos are stored')

    fireEvent.change(screen.getByLabelText('Backend'), { target: { value: 'gcs' } })
    fireEvent.change(screen.getByLabelText('GCS bucket'), { target: { value: 'acme-photos' } })
    expect(screen.getByText(/Add the bucket’s service-account JSON key/)).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Service-account JSON key'), {
      target: { value: '{"type":"service_account"}' },
    })
    expect(screen.queryByText(/Add the bucket’s service-account JSON key/)).toBeNull()
    fireEvent.click(saveButton())

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith('acme', {
        storage: {
          photo_storage_backend: 'gcs',
          gcs_photo_bucket: 'acme-photos',
          gcs_photo_credentials_json: '{"type":"service_account"}',
        },
      }),
    )
  })

  it('never shows a stored key, and goes back to the platform storage as a group', async () => {
    const { update } = renderStorage(ownBucket())
    const key = await screen.findByLabelText('Service-account JSON key')
    expect(key).toHaveValue('')
    expect(key).toHaveAttribute('placeholder', 'Configured — leave blank to keep')

    fireEvent.click(screen.getByRole('button', { name: /Use the platform/ }))
    fireEvent.click(saveButton())

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith('acme', {
        storage: {
          photo_storage_backend: null,
          gcs_photo_bucket: null,
          gcs_photo_credentials_json: null,
        },
      }),
    )
  })

  it("blocks a content type or a size the operator does not allow", async () => {
    const { update } = renderStorage()
    await screen.findByText('Where photos are stored')

    fireEvent.change(screen.getByLabelText('Allowed content types'), {
      target: { value: 'image/png, image/svg+xml' },
    })
    expect(screen.getByText('Not allowed by the operator: image/svg+xml.')).toBeInTheDocument()
    expect(saveButton()).toBeDisabled()

    fireEvent.change(screen.getByLabelText('Allowed content types'), {
      target: { value: 'image/png' },
    })
    fireEvent.change(screen.getByLabelText('Largest photo'), { target: { value: '50' } })
    expect(screen.getByText("The operator's maximum is 10.")).toBeInTheDocument()
    expect(saveButton()).toBeDisabled()

    fireEvent.change(screen.getByLabelText('Largest photo'), { target: { value: '5' } })
    fireEvent.click(saveButton())
    await waitFor(() =>
      expect(update).toHaveBeenCalledWith('acme', {
        storage: { photo_allowed_mime: 'image/png', photo_max_size_mb: 5 },
      }),
    )
  })
})
