/**
 * The words both settings trees use for one stored value: its label, its unit
 * and its choices.
 *
 * On a self-hosted instance the default organization's Email, AI, Semantic
 * search, Photos and Limits pages (`settings-area/org-settings/`) write the
 * very values Platform › Mail relay, AI & search, Storage and Runtime edit
 * (this folder). Each tree had spelled them its own way ("Photo max size" on
 * one page, "Largest photo" on the other, "Timeout seconds" beside "Timeout"
 * and "s"), so one value read as two. Both now take them from here.
 *
 * The search-embedding provider, model, base URL, API key and dimensions are
 * left out on purpose: the Platform page shows them beside the AI provider's
 * own Model, Base URL and API key, so they carry an "Embedding" prefix there
 * that the Organization page, which has nothing else on it, does not need.
 * Only the on/off switch (`search_embeddings_enabled`) is shared.
 */
export const FIELD_COPY = {
  smtp_host: { label: 'SMTP host' },
  smtp_port: { label: 'Port' },
  smtp_security: { label: 'Security' },
  smtp_username: { label: 'SMTP username' },
  smtp_password: { label: 'SMTP password' },
  smtp_from_address: { label: 'Default From address' },
  ai_enabled: { label: 'AI enabled' },
  ai_base_url: { label: 'Base URL' },
  ai_model: { label: 'Model' },
  ai_api_key: { label: 'API key' },
  ai_timeout_seconds: { label: 'Timeout', suffix: 'seconds' },
  ai_max_output_tokens: { label: 'Max output tokens' },
  describe_system_prompt: { label: 'Describe prompt' },
  ask_system_prompt: { label: 'Ask prompt' },
  alert_explanation_system_prompt: { label: 'Alert explanation prompt' },
  search_embeddings_enabled: { label: 'Semantic search enabled' },
  photo_storage_backend: { label: 'Backend' },
  photo_max_size_mb: { label: 'Largest photo', suffix: 'MB' },
  photo_allowed_mime: { label: 'Allowed content types', hint: 'Comma-separated.' },
  gcs_photo_bucket: { label: 'GCS bucket' },
  gcs_photo_public: {
    label: 'Public URLs',
    hint: 'On, photos load from the bucket’s public address; off, from signed links that expire.',
  },
  gcs_photo_signed_url_ttl_seconds: { label: 'Signed link lifetime', suffix: 'seconds' },
  scan_row_limit_default: { label: 'Scan row limit default', suffix: 'rows' },
  metrics_row_limit_default: { label: 'Metrics row limit default', suffix: 'rows' },
} as const satisfies Record<string, { label: string; suffix?: string; hint?: string }>

/** The three system prompts, in the order both AI pages list them. */
export const PROMPT_FIELDS = [
  'describe_system_prompt',
  'ask_system_prompt',
  'alert_explanation_system_prompt',
] as const

/** The title of the card holding the relay's fields, the same on both pages. */
export const SMTP_CARD_TITLE = 'SMTP relay'

// Ports named in the labels because the mode and the port have to agree, and
// disagreeing does not produce an error the operator can act on: the client
// waits for a greeting that never arrives and stalls until it times out.
// Short enough to show whole; the hint under the select says what each mode
// does.
export const SMTP_SECURITY_OPTIONS = [
  { value: 'starttls', label: 'STARTTLS (ports 587, 2525)' },
  { value: 'implicit_tls', label: 'Implicit TLS (port 465)' },
  { value: 'none', label: 'None (plaintext)' },
] as const

export const SMTP_SECURITY_HINTS: Readonly<Record<string, string>> = {
  starttls:
    'Connects in the clear, reads the server greeting, then upgrades. What submission ports 587 and 2525 expect.',
  implicit_tls:
    'Wraps the connection in TLS before sending anything, so the greeting itself is encrypted. What port 465 expects.',
  none: 'No encryption at all. Only reasonable for a relay on localhost or a network path you already trust.',
}

/** Where photos can be written, in one order on both pages. */
export const PHOTO_BACKEND_OPTIONS = [
  { value: 'local', label: "The server's disk" },
  { value: 'gcs', label: 'Google Cloud Storage bucket' },
] as const

/**
 * The note on the fields of the photo backend that is not selected: they stay
 * editable (preparing a bucket before switching is valid), but nothing reads
 * them.
 */
export function inactiveBackendNote(selected: string): string {
  const where = selected === 'gcs' ? 'the Google Cloud Storage bucket' : "the server's disk"
  return `Inactive — photos go to ${where}, so these fields are not used until you switch the backend.`
}

/**
 * The search-embedding provider as a person reads it. tripl speaks one
 * embeddings API, OpenAI's, to any endpoint that offers it; any other value
 * makes the backend embed nothing (`embedding_service.can_embed`), so it is
 * named as unsupported instead of shown as an internal token.
 */
export function embeddingProviderText(provider: string): string {
  if (provider === '' || provider === 'openai') return 'OpenAI-compatible API'
  return `${provider} (unsupported: semantic search embeds nothing)`
}
