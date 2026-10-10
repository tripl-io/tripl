import { describe, expect, it } from 'vitest'
import { orgSettingsFixture } from '@/test/orgSettings'
import {
  AI_ENDPOINT_GROUP,
  EMBEDDING_GROUP,
  SMTP_GROUP,
  STORAGE_GROUP,
  buildOrgUpdate,
  clearGroup,
  displayValue,
  draftInvalid,
  mimeListError,
  groupWarning,
  hasChanges,
  numberError,
  orgSectionForPath,
  withEdit,
} from './orgSettingsModel'

describe('orgSettingsModel', () => {
  it('maps the settings paths onto the four sections', () => {
    expect(orgSectionForPath('organization/email')).toBe('email')
    expect(orgSectionForPath('organization/ai')).toBe('ai')
    expect(orgSectionForPath('organization/search')).toBe('search')
    expect(orgSectionForPath('organization/trackers')).toBeNull()
    expect(orgSectionForPath('organization/limits')).toBe('limits')
    expect(orgSectionForPath('organization/general')).toBeNull()
    expect(orgSectionForPath('instance/email')).toBeNull()
  })

  it('sends numbers as numbers, a clear as null, and never an empty secret', () => {
    expect(
      buildOrgUpdate('ai', {
        ai_timeout_seconds: '30',
        ai_model: null,
        ai_api_key: '   ',
        ai_enabled: false,
      }),
    ).toEqual({ ai: { ai_timeout_seconds: 30, ai_model: null, ai_enabled: false } })
    expect(buildOrgUpdate('email', { smtp_password: 's3cret' })).toEqual({
      email: { smtp_password: 's3cret' },
    })
    expect(hasChanges(buildOrgUpdate('email', {}))).toBe(false)
  })

  it('drops an edit typed back to the saved value', () => {
    const settings = orgSettingsFixture()
    const edited = withEdit(settings, {}, 'limits', 'scan_row_limit_default', '1000')
    expect(edited).toEqual({ scan_row_limit_default: '1000' })
    expect(withEdit(settings, edited, 'limits', 'scan_row_limit_default', '20000')).toEqual({})
    expect(withEdit(settings, {}, 'ai', 'ai_api_key', '')).toEqual({})
  })

  it('shows the inherited value once the organization value is cleared', () => {
    const settings = orgSettingsFixture()
    expect(displayValue(settings, {}, 'limits', 'scan_row_limit_default')).toBe(20000)
    expect(displayValue(settings, { scan_row_limit_default: null }, 'limits', 'scan_row_limit_default')).toBe(
      50000,
    )
  })

  it("refuses a value above the operator's ceiling, only in an organization's scope", () => {
    const settings = orgSettingsFixture()
    expect(numberError('scan_row_limit_default', '60000', settings)).toBe("The platform's maximum is 50,000.")
    expect(numberError('ai_timeout_seconds', '60', settings)).toBeNull()
    expect(numberError('ai_timeout_seconds', '0', settings)).toMatch(/at least 1/)
    expect(numberError('smtp_port', '70000', settings)).toMatch(/65535/)
    expect(draftInvalid(settings, { metrics_row_limit_default: '100001' })).toBe(true)
    // A self-hosted default organization IS the operator: no ceiling above it.
    const operator = orgSettingsFixture({ scope: 'operator' })
    expect(numberError('scan_row_limit_default', '60000', operator)).toBeNull()
  })

  it('warns when an endpoint is set without a key of its own (critique #13)', () => {
    const settings = orgSettingsFixture()
    expect(groupWarning(settings, {}, 'ai')).toBeNull()
    expect(groupWarning(settings, { ai_base_url: 'https://llm.acme.example/v1' }, 'ai')).toEqual({
      starting: true,
      missingSecret: true,
    })
    expect(
      groupWarning(settings, { ai_base_url: 'https://llm.acme.example/v1', ai_api_key: 'sk-acme' }, 'ai'),
    ).toEqual({ starting: true, missingSecret: false })
    expect(groupWarning(settings, { smtp_from_address: 'alerts@acme.example' }, 'email')).toEqual({
      starting: true,
      missingSecret: true,
    })
  })

  it('warns nothing about groups in the operator scope', () => {
    const operator = orgSettingsFixture({ scope: 'operator' })
    expect(groupWarning(operator, { ai_base_url: 'http://localhost:11434' }, 'ai')).toBeNull()
  })

  it('clears only the members the organization owns when going back to the operator', () => {
    const settings = orgSettingsFixture({
      sources: {
        ...orgSettingsFixture().sources,
        'ai.ai_base_url': 'org',
        'ai.ai_api_key': 'org',
        'ai.ai_model': 'default',
      },
    })
    expect(clearGroup({ ai_model: 'x' }, settings, 'ai', AI_ENDPOINT_GROUP)).toEqual({
      ai_base_url: null,
      ai_api_key: null,
    })
    expect(clearGroup({}, orgSettingsFixture(), 'email', SMTP_GROUP)).toEqual({})
  })

  it('treats the embedding endpoint as one credential group with a write-only key (F20 PR10)', () => {
    const settings = orgSettingsFixture()
    expect(buildOrgUpdate('search', { search_embedding_api_key: '  ', search_embeddings_enabled: false })).toEqual({
      search: { search_embeddings_enabled: false },
    })
    expect(withEdit(settings, {}, 'search', 'search_embedding_api_key', '')).toEqual({})
    expect(displayValue(settings, {}, 'search', 'search_embedding_api_key')).toBe('')
    expect(groupWarning(settings, {}, 'search')).toBeNull()
    expect(groupWarning(settings, { search_embedding_model: 'acme-embed' }, 'search')).toEqual({
      starting: true,
      missingSecret: true,
    })
    expect(
      groupWarning(settings, { search_embedding_model: 'acme-embed', search_embedding_api_key: 'sk' }, 'search'),
    ).toEqual({ starting: true, missingSecret: false })
    // The switch is not a group member: turning search off takes nothing over.
    expect(groupWarning(settings, { search_embeddings_enabled: false }, 'search')).toBeNull()
    const owned = orgSettingsFixture({
      sources: { ...settings.sources, 'search.search_embedding_model': 'org' },
    })
    expect(clearGroup({}, owned, 'search', EMBEDDING_GROUP)).toEqual({ search_embedding_model: null })
  })

  it('treats photo storage as one group with a write-only key, within the operator limits (F20 PR11)', () => {
    const settings = orgSettingsFixture()
    expect(orgSectionForPath('organization/storage')).toBe('storage')
    expect(groupWarning(settings, { gcs_photo_bucket: 'acme-photos' }, 'storage')).toEqual({
      starting: true,
      missingSecret: true,
    })
    expect(
      groupWarning(settings, { gcs_photo_bucket: 'acme-photos', gcs_photo_credentials_json: '{}' }, 'storage'),
    ).toEqual({ starting: true, missingSecret: false })
    // The size cap and content types are not part of the group.
    expect(groupWarning(settings, { photo_max_size_mb: '5' }, 'storage')).toBeNull()
    expect(buildOrgUpdate('storage', { gcs_photo_credentials_json: '', photo_max_size_mb: '5' })).toEqual({
      storage: { photo_max_size_mb: 5 },
    })
    expect(draftInvalid(settings, { photo_max_size_mb: '11' })).toBe(true)
    expect(draftInvalid(settings, { gcs_photo_signed_url_ttl_seconds: '30' })).toBe(true)
    expect(draftInvalid(settings, { photo_allowed_mime: 'image/png' })).toBe(false)
    expect(draftInvalid(settings, { photo_allowed_mime: 'image/svg+xml' })).toBe(true)
    expect(mimeListError(' , ', ['image/png'])).toMatch(/at least one/)
    expect(mimeListError('IMAGE/PNG', ['image/png'])).toBeNull()
    const owned = orgSettingsFixture({
      sources: { ...settings.sources, 'storage.gcs_photo_bucket': 'org' },
    })
    expect(clearGroup({}, owned, 'storage', STORAGE_GROUP)).toEqual({ gcs_photo_bucket: null })
  })
})
