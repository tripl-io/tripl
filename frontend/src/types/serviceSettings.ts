import type { components } from './api.gen'

// The Platform settings payloads (`/platform/settings`), taken from the
// generated OpenAPI schema (`api.gen.ts`) rather than restated, so a backend
// change is a compile error where it is read. A field the backend declares
// with a `None` default is optional here even though the server always sends
// it: read it with `== null` or `??`.
type Schemas = components['schemas']

export type ServiceSettings = Schemas['ServiceSettingsResponse']

/**
 * Where a setting's value came from. `default` means it equals the built-in
 * default — either nothing was delivered for it, or what was delivered happens
 * to match; from the running process the two are indistinguishable. The badge
 * used to answer `env` for every field with no stored override, which made it
 * useless as evidence that a variable had reached the container.
 *
 * The backend's one `SettingSource` also carries `org` and `disabled`, which
 * only the organization view sends (`OrgSettingSource`); the Platform view
 * answers `env`, `override` or `default`.
 */
export type SettingSource = ServiceSettings['sources'][string]

export type RuntimeSettings = Schemas['RuntimeSettings']
export type SecuritySettings = Schemas['SecuritySettings']
export type StorageSettings = Schemas['StorageSettings']
export type ObservabilitySettings = Schemas['ObservabilitySettings']
export type EmailSettings = Schemas['EmailSettings']

/** Self-service registration policy (backend `RegistrationMode`). `open` lets
 *  anyone who can reach the instance create an account; `disabled` refuses new
 *  signups apart from the first-owner bootstrap on an empty instance. */
export type RegistrationMode = SecuritySettings['registration_mode']

/** How the SMTP client secures the connection (backend `SmtpSecurity`). Three
 *  different protocols, not three strengths of one: `starttls` connects in the
 *  clear and upgrades after the greeting (ports 587/2525), `implicit_tls` wraps
 *  the socket before a byte is sent (SMTPS, port 465), `none` stays plaintext.
 *  Mismatching the mode and the port does not error — it stalls. */
export type SmtpSecurity = EmailSettings['smtp_security']

/**
 * The AI section (backend `AiSettings`). `search_embedding_base_url` is
 * read-only, and absent from `AiSettingsUpdate` on purpose: this is where
 * indexed plan text is POSTed, and repointing it at runtime would poison every
 * vector already in the index.
 */
export type AiServiceSettings = Schemas['AiSettings']

/**
 * What the process found at startup. `version` is the server's package
 * version (`tripl.__version__`); `edition` is `enterprise` when an extension
 * is installed. `alembic_revision` is the revision stamped in the database's
 * `alembic_version` table, null when it could not be read (no row, no table,
 * or the DB is unreachable); `alembic_head_revision` the migration head this
 * build ships, null if the script directory is unreadable;
 * `alembic_up_to_date` null whenever either side is unknown — never guessed.
 * The settings response's `system` is null for everyone but a platform admin:
 * the process environment and migration state are operator information.
 */
export type SystemSettings = Schemas['SystemSettings']

export type RuntimeSettingsUpdate = Schemas['RuntimeSettingsUpdate']
export type SecuritySettingsUpdate = Schemas['SecuritySettingsUpdate']
export type StorageSettingsUpdate = Schemas['StorageSettingsUpdate']
export type ObservabilitySettingsUpdate = Schemas['ObservabilitySettingsUpdate']
export type EmailSettingsUpdate = Schemas['EmailSettingsUpdate']
export type AiSettingsUpdate = Schemas['AiSettingsUpdate']
export type ServiceSettingsUpdate = Schemas['ServiceSettingsUpdate']

export type SettingsTestResponse = Schemas['SettingsTestResponse']
