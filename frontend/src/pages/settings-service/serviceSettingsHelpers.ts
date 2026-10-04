import type {
  AiServiceSettings,
  EmailSettings,
  ObservabilitySettings,
  RuntimeSettings,
  SecuritySettings,
  ServiceSettings,
  ServiceSettingsUpdate,
  StorageSettings,
} from '@/types'

export type SectionKey = 'runtime' | 'ai' | 'email' | 'security' | 'storage' | 'observability'

/** Section names as the rail and the page header spell them. */
export const SECTION_LABELS: Record<SectionKey, string> = {
  runtime: 'Runtime',
  ai: 'AI',
  email: 'Email',
  security: 'Security & access',
  storage: 'Storage',
  observability: 'Observability',
}

/** The three secrets that are stored encrypted and never read back. */
export type SecretField = 'ai_api_key' | 'search_embedding_api_key' | 'smtp_password'

/**
 * A section as the form holds it: every numeric field may also be the raw text
 * of its input. Converting on each keystroke turned an emptied field into
 * `Number('') === 0`, so clearing "30" to type "45" showed "0", and a save in
 * between sent a 0s timeout or port 0. The text is kept as typed and converted
 * (or refused) by buildUpdate / numberFieldError.
 */
type NumericDraft<T> = { [K in keyof T]: T[K] extends number ? number | string : T[K] }

export type EditableSettings = {
  runtime: NumericDraft<RuntimeSettings>
  ai: NumericDraft<AiServiceSettings>
  email: NumericDraft<EmailSettings>
  security: NumericDraft<SecuritySettings>
  storage: NumericDraft<StorageSettings>
  observability: NumericDraft<ObservabilitySettings>
}

export type SecretDrafts = {
  ai_api_key: string
  search_embedding_api_key: string
  smtp_password: string
}

export const EMPTY_SECRET_DRAFTS: SecretDrafts = {
  ai_api_key: '',
  search_embedding_api_key: '',
  smtp_password: '',
}

export const RESET_FIELDS: Record<SectionKey, readonly string[]> = {
  runtime: ['app_base_url', 'scan_row_limit_default', 'metrics_row_limit_default'],
  ai: [
    'ai_enabled',
    'ai_base_url',
    'ai_model',
    'ai_api_key',
    'ai_timeout_seconds',
    'ai_max_output_tokens',
    'describe_system_prompt',
    'ask_system_prompt',
    'alert_explanation_system_prompt',
    'search_embeddings_enabled',
    'search_embedding_provider',
    'search_embedding_model',
    'search_embedding_api_key',
  ],
  email: [
    'smtp_host',
    'smtp_port',
    'smtp_username',
    'smtp_password',
    'smtp_security',
    'smtp_from_address',
  ],
  security: [
    'registration_mode',
    'cors_allow_origins',
    'session_cookie_name',
    'session_ttl_hours',
    'session_cookie_secure',
    'security_headers_enabled',
    'hsts_enabled',
    'hsts_max_age_seconds',
    'content_security_policy',
    'rate_limit_enabled',
    'rate_limit_login_per_minute',
    'rate_limit_register_per_hour',
    'rate_limit_trust_forwarded_for',
  ],
  storage: [
    'photo_storage_backend',
    'photo_local_dir',
    'photo_max_size_mb',
    'photo_allowed_mime',
    'gcs_photo_bucket',
    'gcs_photo_credentials_path',
    'gcs_photo_public',
    'gcs_photo_signed_url_ttl_seconds',
  ],
  observability: [
    'request_id_header',
    'log_level',
    'log_json',
    'prometheus_metrics_enabled',
    'otel_exporter_otlp_endpoint',
    'otel_service_name',
  ],
}

export const COMPARE_FIELDS: Record<SectionKey, readonly string[]> = {
  ...RESET_FIELDS,
  ai: RESET_FIELDS.ai.filter(
    field => field !== 'ai_api_key' && field !== 'search_embedding_api_key',
  ),
  email: RESET_FIELDS.email.filter(field => field !== 'smtp_password'),
}

type NumberRule = { min: number; max?: number }

/**
 * The editable numeric fields and the range the backend accepts for each
 * (backend/src/tripl/schemas/app_settings.py). Out-of-range values used to go
 * out and come back as a 422 in the save row, or be stored as nonsense.
 */
export const NUMBER_FIELDS: Record<SectionKey, Readonly<Record<string, NumberRule>>> = {
  runtime: { scan_row_limit_default: { min: 1 }, metrics_row_limit_default: { min: 1 } },
  ai: { ai_timeout_seconds: { min: 1 }, ai_max_output_tokens: { min: 1 } },
  email: { smtp_port: { min: 1, max: 65535 } },
  security: {
    session_ttl_hours: { min: 1 },
    hsts_max_age_seconds: { min: 0 },
    rate_limit_login_per_minute: { min: 0 },
    rate_limit_register_per_hour: { min: 0 },
  },
  storage: { photo_max_size_mb: { min: 1 }, gcs_photo_signed_url_ttl_seconds: { min: 1 } },
  observability: {},
}

/** Why a numeric field's current text cannot be saved, or null when it can. */
export function numberFieldError(
  section: SectionKey,
  field: string,
  value: unknown,
): string | null {
  const rule = NUMBER_FIELDS[section][field]
  if (!rule) return null
  const text = String(value ?? '').trim()
  const range =
    rule.max === undefined ? `${rule.min} or more` : `from ${rule.min} to ${rule.max}`
  if (text === '') return `Enter a whole number, ${range}.`
  const number = Number(text)
  if (!Number.isInteger(number) || number < rule.min || (rule.max !== undefined && number > rule.max)) {
    return `Enter a whole number, ${range}.`
  }
  return null
}

/**
 * True when `update` would send a numeric field of `section` that cannot be
 * saved. Only edited fields count: a value delivered by the environment is
 * whatever the backend accepted, and blocking an unrelated edit on it would
 * leave Save dead until the owner overrode a field they never touched.
 */
export function updateHasInvalidNumber(update: ServiceSettingsUpdate, section: SectionKey): boolean {
  const values = (update[section] ?? {}) as Record<string, unknown>
  return Object.keys(values).some(field => numberFieldError(section, field, values[field]) !== null)
}

export function editableFromSettings(settings: ServiceSettings): EditableSettings {
  return {
    runtime: { ...settings.runtime },
    ai: { ...settings.ai },
    email: { ...settings.email },
    security: { ...settings.security },
    storage: { ...settings.storage },
    observability: { ...settings.observability },
  }
}

/**
 * Refresh ONE section from the server's stored settings, leaving every other
 * section's in-progress edits alone.
 *
 * Reset and Clear write straight through and get the whole settings object
 * back; adopting it wholesale replaced a `form` that spans all six sections, so
 * clearing the AI key also discarded an unsaved Storage edit or a rewritten
 * prompt — the very draft the leave-guard warns about.
 */
export function adoptSection(
  form: EditableSettings,
  settings: ServiceSettings,
  section: SectionKey,
): EditableSettings {
  return {
    ...form,
    [section]: { ...settings[section] },
  } as EditableSettings
}

/**
 * Same, but keeping every field the user can edit as it stands on screen.
 *
 * Clearing a stored secret flips server-owned state inside the section it
 * touches (`ai_api_key_configured`), so the response has to be adopted — but
 * the prompt being written in the field below it is the user's, not the
 * server's, and reverting it is the loss the leave-guard warns about.
 */
export function adoptSectionKeepingEdits(
  form: EditableSettings,
  settings: ServiceSettings,
  section: SectionKey,
): EditableSettings {
  const merged = { ...settings[section] } as unknown as Record<
    string,
    string | number | boolean | undefined
  >
  const edited = form[section] as unknown as Record<string, string | number | boolean | undefined>
  for (const field of COMPARE_FIELDS[section]) {
    merged[field] = edited[field]
  }
  return { ...form, [section]: merged } as EditableSettings
}

/**
 * Blank the secret drafts belonging to `section`, leaving the other sections'
 * alone. A section reset nulls its stored secrets server-side (they are in
 * RESET_FIELDS), so a replacement typed into one of those boxes is part of what
 * the user just asked to clear — while a password typed on Email is not.
 */
export function clearSectionSecrets(drafts: SecretDrafts, section: SectionKey): SecretDrafts {
  const next: SecretDrafts = { ...drafts }
  for (const field of RESET_FIELDS[section]) {
    if (field in next) next[field as SecretField] = ''
  }
  return next
}

export function sourceFor(
  settings: ServiceSettings | undefined,
  section: SectionKey,
  field: string,
) {
  // The fallback only fires when the server omitted the key. Of the three,
  // 'default' is the only one that does not assert something was delivered.
  return settings?.sources[`${section}.${field}`] ?? 'default'
}

/**
 * How many of this section's fields are actually overridden on this instance —
 * counted exactly the way the row badges count it, so the two can never
 * disagree in the same viewport.
 *
 * That holds only while EVERY field in RESET_FIELDS renders a SourceBadge. The
 * three stored secrets are in this count because a reset genuinely nulls them,
 * and they were the one gap: with only an SMTP password stored, the red card
 * read "Clears the 1 Email override — every field badged Override above"
 * beside five rows all badged "Default". The fix is
 * the badge on the secret row, not a narrower count — a narrower count would
 * claim "Nothing to clear" next to a reset that does clear the stored key.
 */
export function overrideCount(
  settings: ServiceSettings | undefined,
  section: SectionKey,
): number {
  return RESET_FIELDS[section].filter(
    field => sourceFor(settings, section, field) === 'override',
  ).length
}

/**
 * What a section reset would actually clear.
 *
 * This used to interpolate `RESET_FIELDS[section].length` — the number of
 * RESETTABLE fields, not overridden ones — so a fresh instance whose every row
 * was badged "Env" or "Default" was told in red that it had 6 Email overrides
 * to clear, next to a live button that would have done nothing.
 *
 * The copy names both non-override badges for the same reason: saying every
 * field "comes from an environment variable" beside rows badged "Default" would
 * be the same disagreement in a second viewport.
 */
export function resetCardDescription(section: SectionKey, overrides: number): string {
  const label = SECTION_LABELS[section]
  if (overrides === 0) {
    return `Nothing to clear: no ${label} field on this instance is badged "Override" — every one already comes from the environment or its built-in default.`
  }
  const noun = overrides === 1 ? 'override' : 'overrides'
  return `Clears the ${overrides} ${label} ${noun} on this instance at once — every field badged "Override" above falls back to its environment variable, or to its built-in default where none is set.`
}

export function buildSectionDiff(
  section: SectionKey,
  fields: readonly string[],
  current: EditableSettings,
  saved: ServiceSettings,
): Record<string, string | number | boolean> {
  const changes: Record<string, string | number | boolean> = {}
  const currentSection = current[section] as unknown as Record<string, string | number | boolean>
  const savedSection = saved[section] as unknown as Record<string, string | number | boolean>
  for (const field of fields) {
    let value = currentSection[field]
    if (value === undefined) continue
    // A numeric field typed back to its saved value is not a change, and a
    // valid one goes out as a number, not as the input's text.
    if (
      typeof value === 'string' &&
      NUMBER_FIELDS[section][field] &&
      numberFieldError(section, field, value) === null
    ) {
      value = Number(value)
    }
    if (value !== savedSection[field]) {
      changes[field] = value
    }
  }
  return changes
}

export function buildUpdate(
  form: EditableSettings | null,
  saved: ServiceSettings | undefined,
  secretDrafts: SecretDrafts,
): ServiceSettingsUpdate {
  if (!form || !saved) return {}
  const update: ServiceSettingsUpdate = {}

  for (const section of Object.keys(COMPARE_FIELDS) as SectionKey[]) {
    const diff = buildSectionDiff(section, COMPARE_FIELDS[section], form, saved)
    if (Object.keys(diff).length > 0) {
      update[section] = diff as never
    }
  }

  if (secretDrafts.ai_api_key.trim()) {
    update.ai = { ...(update.ai ?? {}), ai_api_key: secretDrafts.ai_api_key.trim() }
  }
  if (secretDrafts.search_embedding_api_key.trim()) {
    update.ai = {
      ...(update.ai ?? {}),
      search_embedding_api_key: secretDrafts.search_embedding_api_key.trim(),
    }
  }
  if (secretDrafts.smtp_password.trim()) {
    update.email = {
      ...(update.email ?? {}),
      smtp_password: secretDrafts.smtp_password,
    }
  }

  return update
}

export function hasUpdate(update: ServiceSettingsUpdate) {
  return Object.values(update).some(section => section && Object.keys(section).length > 0)
}

/** The part of `update` that belongs to one section. */
export function pickSection(update: ServiceSettingsUpdate, section: SectionKey): ServiceSettingsUpdate {
  const part = update[section]
  return part && Object.keys(part).length > 0 ? ({ [section]: part } as ServiceSettingsUpdate) : {}
}

/** Sections with unsaved edits, in rail order. */
export function dirtySections(update: ServiceSettingsUpdate): SectionKey[] {
  return (Object.keys(SECTION_LABELS) as SectionKey[]).filter(
    section => Object.keys(update[section] ?? {}).length > 0,
  )
}

/**
 * The consequences of a Security save that can lock people out or break this
 * app, one sentence each; empty when the save touches none of them. Reset and
 * Clear were confirm-gated while Save was not, even though Save is how public
 * signup gets opened or a Secure-only cookie lands on an HTTP deployment.
 */
export function lockoutRisks(update: ServiceSettingsUpdate): string[] {
  const security = (update.security ?? {}) as Record<string, unknown>
  const risks: string[] = []
  if (security.registration_mode === 'open') {
    risks.push(
      'Self-service registration opens at once: anyone who can reach this instance can create an account.',
    )
  }
  if ('session_cookie_name' in security) {
    risks.push('Renaming the session cookie signs everyone out after the next restart.')
  }
  if (security.session_cookie_secure === true) {
    risks.push(
      'A Secure-only session cookie is never sent over plain HTTP: if this instance is served over HTTP, nobody can sign in after the next restart.',
    )
  }
  if ('content_security_policy' in security) {
    risks.push(
      'A Content Security Policy that blocks this app’s own scripts or API calls stops the app from loading after the next restart.',
    )
  }
  if ('cors_allow_origins' in security) {
    risks.push(
      'Browser clients on origins missing from the CORS list are refused after the next restart.',
    )
  }
  return risks
}

export function resetPayload(section: SectionKey): ServiceSettingsUpdate {
  return {
    [section]: Object.fromEntries(RESET_FIELDS[section].map(field => [field, null])),
  } as ServiceSettingsUpdate
}

/**
 * When a saved override starts being obeyed, per section. Mirrors
 * STARTUP_APPLIED_FIELDS in backend/src/tripl/services/app_settings_service.py:
 * runtime/email/ai are read through build_*_config at request/task time, while
 * security/storage/observability are read once at startup — with
 * registration_mode the one security field applied live. The UI used to promise
 * the exact opposite (a redeploy note on Runtime, which needs none, and silence
 * on Storage and Observability, which do).
 */
// One line each: the sticky bar held a 3-4 line paragraph, a quarter of a
// phone screen pinned while scrolling. The fallback rule every section
// shares lives once, in SOURCE_LEGEND, not in every bar.
const APPLY_NOTES: Record<SectionKey, string> = {
  runtime: 'Applies to the next request or scan task — no restart needed.',
  email: 'Applies to the next message tripl sends — no restart needed.',
  ai: 'Applies to the next AI call — no restart needed.',
  security: 'Applies after the next restart of the API; Self-service registration applies at once.',
  storage: 'Applies after the next restart of the API and workers.',
  observability: 'Applies after the next restart of the API and workers.',
}

/**
 * What the source badges mean, said once per page above the fields.
 * "Or to the built-in default where none is set" is not hedging: on AI
 * the three system prompts have no environment variable at all — the backend
 * reads them off ai_defaults — and an unset variable falls back to the pydantic
 * default, not to nothing.
 */
export const SOURCE_LEGEND =
  'Fields marked Override are stored here; Env comes from the environment. Unmarked fields fall back to their environment variable, or to the built-in default where none is set.'

export function applyNote(section: SectionKey): string {
  return APPLY_NOTES[section]
}

/** The highest-consequence fields each section reset nulls, named in the confirm. */
const RESET_STAKES: Record<SectionKey, string> = {
  runtime: 'including the app base URL used in invitation links and the ingest endpoint',
  ai: 'including both stored API keys and all three system prompts',
  email: 'including the stored SMTP password',
  security:
    'including Self-service registration — which applies live, so this can reopen public signup — plus the CORS origins, the CSP and the rate limits',
  storage: 'including the photo storage backend, its GCS bucket and its credentials path',
  observability: 'including the log level and the OTLP endpoint',
}

export type ConfirmCopy = { title: string; message: string; confirmLabel: string }

export function resetConfirm(section: SectionKey, hasSectionDraft = false): ConfirmCopy {
  const label = SECTION_LABELS[section]
  // A reset re-reads this section from the server, so an edit made here and not
  // yet saved goes with it. The dialog enumerates every other consequence; it
  // may not stay silent about the one the user can see on screen.
  const draftStake = hasSectionDraft
    ? ` Changes you made in ${label} but have not saved are dropped as well.`
    : ''
  return {
    title: `Reset ${label} to defaults`,
    // "settings", not "overrides": the number is the size of the PATCH — every
    // field this write nulls — and only some of them are overridden right now.
    // The card that opens this dialog counts the real overrides.
    // Same vocabulary as applyNote and resetCardDescription: this dialog names
    // "all three system prompts" in its own stakes line, and none of the three
    // has an environment variable to fall back to.
    message: `Clear all ${RESET_FIELDS[section].length} ${label} settings on this instance — ${RESET_STAKES[section]} — so every one of them falls back to its environment variable, or to its built-in default where none is set. This is saved immediately and cannot be undone.${draftStake}`,
    confirmLabel: 'Reset section',
  }
}

const SECRET_STAKES: Record<SecretField, { name: string; consequence: string }> = {
  ai_api_key: {
    name: 'AI API key',
    consequence: 'Anomaly explanations, schema suggestions and the assistant start failing at once.',
  },
  search_embedding_api_key: {
    name: 'embedding API key',
    consequence: 'Semantic search stops embedding and answering at once.',
  },
  smtp_password: {
    name: 'SMTP password',
    consequence: 'Invitations, alerts and digests stop being delivered at once.',
  },
}

export function clearSecretConfirm(field: SecretField, hasFieldDraft = false): ConfirmCopy {
  const { name, consequence } = SECRET_STAKES[field]
  const draftStake = hasFieldDraft
    ? ' The replacement you typed into this field and have not saved is cleared too.'
    : ''
  return {
    title: `Delete the stored ${name}`,
    // The field beside this button waits for "Save changes"; the button does
    // not — it writes straight through to the server.
    message: `The stored ${name} is deleted on the server as soon as you confirm — this does not wait for Save changes, and it cannot be undone. ${consequence}${draftStake}`,
    confirmLabel: 'Delete',
  }
}
