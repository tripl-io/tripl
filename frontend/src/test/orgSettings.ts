import type { OrgSettings, OrgSettingsValues } from '@/api/orgSettings'

/** Test fixture: an organization with some values of its own (F20 PR9). */
const OPERATOR_VALUES: OrgSettingsValues = {
  limits: { scan_row_limit_default: 50000, metrics_row_limit_default: 100000 },
  email: {
    smtp_host: 'smtp.operator.example',
    smtp_port: 587,
    smtp_username: 'ops',
    smtp_password_configured: true,
    smtp_security: 'starttls',
    smtp_from_address: 'tripl@operator.example',
  },
  ai: {
    ai_enabled: true,
    ai_base_url: 'https://api.operator.example/v1',
    ai_model: 'gpt-operator',
    ai_api_key_configured: true,
    ai_timeout_seconds: 60,
    ai_max_output_tokens: 2000,
    describe_system_prompt: 'Describe.',
    ask_system_prompt: 'Ask.',
    alert_explanation_system_prompt: 'Explain.',
  },
}

export function orgSettingsFixture(overrides: Partial<OrgSettings> = {}): OrgSettings {
  return {
    organization: 'acme',
    scope: 'organization',
    operator_fallback: 'all',
    ...OPERATOR_VALUES,
    // The organization lowered its scan cap; everything else is inherited.
    limits: { scan_row_limit_default: 20000, metrics_row_limit_default: 100000 },
    inherited: OPERATOR_VALUES,
    ceilings: {
      scan_row_limit_default: 50000,
      metrics_row_limit_default: 100000,
      ai_timeout_seconds: 60,
      ai_max_output_tokens: 2000,
    },
    overridden_fields: ['scan_row_limit_default'],
    sources: {
      'limits.scan_row_limit_default': 'org',
      'limits.metrics_row_limit_default': 'override',
      'email.smtp_host': 'override',
      'email.smtp_port': 'env',
      'email.smtp_username': 'override',
      'email.smtp_password': 'override',
      'email.smtp_security': 'default',
      'email.smtp_from_address': 'override',
      'ai.ai_enabled': 'override',
      'ai.ai_base_url': 'override',
      'ai.ai_model': 'override',
      'ai.ai_api_key': 'override',
      'ai.ai_timeout_seconds': 'default',
      'ai.ai_max_output_tokens': 'default',
      'ai.describe_system_prompt': 'default',
      'ai.ask_system_prompt': 'default',
      'ai.alert_explanation_system_prompt': 'default',
    },
    ...overrides,
  }
}
