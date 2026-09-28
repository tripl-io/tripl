import type { OrgSettings, OrgSettingsValues } from '@/api/orgSettings'

/** Test fixture: an organization with some values of its own (F20 PR9-PR11). */
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
  search: {
    search_embeddings_enabled: true,
    search_embedding_provider: 'openai',
    search_embedding_model: 'text-embedding-3-small',
    // The operator's endpoint is never shown to an organization's admins.
    search_embedding_base_url: '',
    search_embedding_api_key_configured: true,
    search_embedding_dimensions: 1536,
  },
  storage: {
    photo_storage_backend: 'local',
    photo_max_size_mb: 10,
    photo_allowed_mime: 'image/jpeg,image/png,image/gif,image/webp',
    // The platform's bucket is never shown to an organization's admins.
    gcs_photo_bucket: '',
    gcs_photo_credentials_configured: false,
    gcs_photo_public: false,
    gcs_photo_signed_url_ttl_seconds: 3600,
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
      photo_max_size_mb: 10,
    },
    storage_limits: {
      operator_allowed_mime: ['image/jpeg', 'image/png', 'image/gif', 'image/webp'],
      local_backend_allowed: false,
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
      'search.search_embeddings_enabled': 'override',
      'search.search_embedding_provider': 'default',
      'search.search_embedding_model': 'env',
      'search.search_embedding_base_url': 'override',
      'search.search_embedding_api_key': 'override',
      'storage.photo_storage_backend': 'default',
      'storage.photo_max_size_mb': 'default',
      'storage.photo_allowed_mime': 'default',
      'storage.gcs_photo_bucket': 'default',
      'storage.gcs_photo_credentials_json': 'default',
      'storage.gcs_photo_public': 'default',
      'storage.gcs_photo_signed_url_ttl_seconds': 'default',
    },
    ...overrides,
  }
}
