import { render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { orgSettingsApi, type OrgSettings } from '@/api/orgSettings'
import { ActiveOrgContext } from '@/components/active-org-context'
import { AuthContext } from '@/components/auth-context'
import {
  PLATFORM_SECTION_KEYS,
  PLATFORM_SECTION_LABELS,
} from '@/components/settings/platform-sections'
import { SOURCE_LEGEND } from '@/pages/settings-service/serviceSettingsHelpers'
import { authAs } from '@/test/auth'
import { orgSettingsFixture } from '@/test/orgSettings'
import InstanceSection from './InstanceSection'
import OrgSettingsSection from './OrgSettingsSection'
import type { OrgSection } from './org-settings/orgSettingsModel'

/**
 * The Organization settings pages read as one set with the Platform pages
 * that hold the same values: one name per page, one note above the fields
 * that is about this page, and nothing that looks editable and is not.
 */

function renderSection(section: OrgSection, settings: OrgSettings = orgSettingsFixture()) {
  vi.spyOn(orgSettingsApi, 'get').mockResolvedValue(settings)
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
}

/** The fixture with every value at its built-in default: a fresh instance. */
function freshSettings(overrides: Partial<OrgSettings> = {}): OrgSettings {
  const base = orgSettingsFixture()
  const sources = Object.fromEntries(Object.keys(base.sources).map(key => [key, 'default' as const]))
  return orgSettingsFixture({ sources, ...overrides })
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Organization settings page names', () => {
  it.each([
    ['storage', 'Photos'],
    ['search', 'Semantic search'],
    ['email', 'Email'],
  ] as const)('heads %s with its rail name, %s', async (section, title) => {
    renderSection(section)

    expect(await screen.findByRole('heading', { level: 1, name: title })).toBeInTheDocument()
  })
})

describe('the note above an Organization settings page', () => {
  it('keeps the account-mail sentence to Email on the self-hosted default organization', async () => {
    renderSection('ai', orgSettingsFixture({ scope: 'operator' }))

    const note = await screen.findByText(/these are the platform’s own settings/)
    expect(note).not.toHaveTextContent(/account mail/)
    // The Platform page that edits the same values.
    expect(note).toHaveTextContent(`Platform › ${PLATFORM_SECTION_LABELS.ai}`)
  })

  it('folds the re-embed warning into the one note on Semantic search', async () => {
    renderSection('search', orgSettingsFixture({ scope: 'operator' }))

    const note = await screen.findByRole('note')
    expect(note).toHaveTextContent(/these are the platform’s own settings/)
    expect(note).toHaveTextContent(/re-embeds this organization’s projects/)
  })

  it('leaves out the badge legend while no field wears a badge', async () => {
    renderSection('limits', freshSettings())

    await screen.findByLabelText('Scan row limit default')
    expect(screen.queryByText(/No badge: the built-in default/)).toBeNull()
  })

  it('explains the badges once some field wears one', async () => {
    renderSection('limits')

    expect(await screen.findByText(/No badge: the built-in default/)).toBeInTheDocument()
  })
})

describe('badges on the self-hosted default organization', () => {
  it('use the Platform pages’ words for the same stored rows', async () => {
    renderSection('email', orgSettingsFixture({ scope: 'operator' }))

    await screen.findByDisplayValue('smtp.operator.example')
    // A row stored in the settings table is "Override" here as on Platform ›
    // Mail relay, not a second name for the same thing.
    expect(screen.getAllByText('Override', { selector: '[title]' }).length).toBeGreaterThan(0)
    expect(screen.queryByText('Platform', { selector: '[title]' })).toBeNull()
    expect(screen.getByText(SOURCE_LEGEND)).toBeInTheDocument()
  })

  it('call an inherited value Platform on an organization of its own', async () => {
    renderSection('email')

    await screen.findByDisplayValue('smtp.operator.example')
    expect(screen.getAllByText('Platform', { selector: '[title]' }).length).toBeGreaterThan(0)
    expect(screen.queryByText(/Operator/)).toBeNull()
  })
})

describe('Organization › Photos', () => {
  it('says the platform store applies after a restart, as Platform › Storage does', async () => {
    renderSection('storage', orgSettingsFixture({ scope: 'operator' }))

    expect(await screen.findByText('Applies after the next restart of the API and workers.')).toBeInTheDocument()
    expect(screen.queryByText(/as soon as it is saved/)).toBeNull()
  })

  it('applies an organization’s own storage on save', async () => {
    renderSection('storage')

    expect(
      await screen.findByText('Takes effect for this organization as soon as it is saved.'),
    ).toBeInTheDocument()
  })

  it('fades the bucket fields while photos go to the server’s disk, and says so', async () => {
    renderSection('storage', orgSettingsFixture({ scope: 'operator' }))

    const bucket = await screen.findByLabelText('GCS bucket')
    expect(bucket.closest('[data-inactive="true"]')).not.toBeNull()
    expect(screen.getByText(/Photos go to the server's disk, in the directory set under Platform › Storage/)).toBeInTheDocument()
    // The platform's key file is a Platform › Storage field, not "not a setting".
    expect(screen.getByText(/GCS credentials path/)).toBeInTheDocument()
  })

  it('leaves the bucket fields live once the backend is a bucket', async () => {
    const base = orgSettingsFixture({ scope: 'operator' })
    renderSection('storage', { ...base, storage: { ...base.storage, photo_storage_backend: 'gcs' } })

    const bucket = await screen.findByLabelText('GCS bucket')
    expect(bucket.closest('[data-inactive]')).toBeNull()
  })
})

describe('Organization › Semantic search', () => {
  it('shows the provider as the API tripl speaks, as text', async () => {
    renderSection('search')

    const provider = await screen.findByRole('group', { name: 'Provider' })
    expect(within(provider).getByText('OpenAI-compatible API')).toBeInTheDocument()
    expect(within(provider).queryByRole('textbox')).toBeNull()
    expect(screen.queryByDisplayValue('openai')).toBeNull()
  })
})

describe('Organization › Limits on the self-hosted default organization', () => {
  it('does not tell the platform it may not raise its own limits', async () => {
    renderSection('limits', orgSettingsFixture({ scope: 'operator' }))

    expect(await screen.findByText(/Other organizations may lower them, never raise them/)).toBeInTheDocument()
    expect(screen.queryByText(/above the platform's/)).toBeNull()
  })
})

describe('Platform section headers', () => {
  it.each(PLATFORM_SECTION_KEYS)('head %s with the rail’s name for it', key => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <AuthContext.Provider value={authAs('owner')}>
          <MemoryRouter>
            <InstanceSection section={key} />
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )

    expect(
      screen.getByRole('heading', { level: 1, name: PLATFORM_SECTION_LABELS[key] }),
    ).toBeInTheDocument()
  })

  it('describes Storage as the photo store it is', () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <AuthContext.Provider value={authAs('owner')}>
          <MemoryRouter>
            <InstanceSection section="storage" />
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )

    expect(screen.getByText(/Where event photos are stored/)).toBeInTheDocument()
    expect(screen.queryByText(/ingested events/)).toBeNull()
  })
})
