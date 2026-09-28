import { describe, expect, it } from 'vitest'
import { orgSettingsFixture } from '@/test/orgSettings'
import {
  AI_ENDPOINT_GROUP,
  SMTP_GROUP,
  buildOrgUpdate,
  clearGroup,
  displayValue,
  draftInvalid,
  groupWarning,
  hasChanges,
  numberError,
  orgSectionForPath,
  withEdit,
} from './orgSettingsModel'

describe('orgSettingsModel', () => {
  it('maps the settings paths onto the three sections', () => {
    expect(orgSectionForPath('organization/email')).toBe('email')
    expect(orgSectionForPath('organization/ai')).toBe('ai')
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
    expect(numberError('scan_row_limit_default', '60000', settings)).toBe("The operator's maximum is 50,000.")
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
})
