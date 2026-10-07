import type {
  ConnectionSettings,
  ConnectionSettingsResponse,
  DatabricksAuthType,
  DbType,
  PostgresSslMode,
  SnowflakeAuthType,
} from '@/types'
import { REQUIRED_MESSAGE } from '@/components/forms/validation'
import { INPUT_INVALID_CLASS, INPUT_PLACEHOLDER_CLASS, INPUT_TEXT_CLASS } from '@/components/settings/input-style'

// Form state for the typed, per-warehouse connection settings. Kept as strings
// (what inputs produce) and converted to the API shape by
// `buildConnectionSettings`, which only ever emits the settings that apply to
// the selected warehouse — the backend rejects the rest with a 422.
export interface ConnectionSettingsForm {
  // BigQuery
  location: string
  maximumBytesBilled: string
  datasetAllowlist: string
  // PostgreSQL
  sslmode: PostgresSslMode | ''
  sslrootcert: string
  sslcert: string
  sslkey: string
  clearSslkey: boolean
  searchPath: string
  // Databricks (schemaName and schemaAllowlist are Snowflake's too)
  httpPath: string
  authType: DatabricksAuthType
  schemaName: string
  schemaAllowlist: string
  // Snowflake
  warehouse: string
  role: string
  snowflakeAuthType: SnowflakeAuthType
}

export const EMPTY_CONNECTION_SETTINGS_FORM: ConnectionSettingsForm = {
  location: '',
  maximumBytesBilled: '',
  datasetAllowlist: '',
  sslmode: '',
  sslrootcert: '',
  sslcert: '',
  sslkey: '',
  clearSslkey: false,
  searchPath: '',
  httpPath: '',
  authType: 'pat',
  schemaName: '',
  schemaAllowlist: '',
  warehouse: '',
  role: '',
  snowflakeAuthType: 'password',
}

export const DATABRICKS_AUTH_OPTIONS: { value: DatabricksAuthType; label: string }[] = [
  { value: 'pat', label: 'Access token (personal or service principal)' },
  { value: 'oauth_m2m', label: 'OAuth machine-to-machine (service principal)' },
]

export const SNOWFLAKE_AUTH_OPTIONS: { value: SnowflakeAuthType; label: string }[] = [
  { value: 'password', label: 'Password' },
  { value: 'key_pair', label: 'Key pair (private key in the secret field)' },
]

// Mirrors MAX_SNOWFLAKE_SCHEMA_ALLOWLIST in backend/src/tripl/schemas/data_source.py.
export const MAX_SNOWFLAKE_SCHEMA_ALLOWLIST = 50

// Mirrors _SF_OBJECT_RE on the backend: a warehouse, role or schema name.
const SNOWFLAKE_OBJECT_RE = /^[A-Za-z0-9_$-]{1,255}$/

/** Why `value` is not a Snowflake warehouse name, or null when it is. */
export function snowflakeWarehouseError(value: string, requiredMessage: string): string | null {
  const trimmed = value.trim()
  if (!trimmed) return requiredMessage
  if (!SNOWFLAKE_OBJECT_RE.test(trimmed)) {
    return 'Use the warehouse name only: letters, digits, _ and $, like COMPUTE_WH.'
  }
  return null
}

// Mirrors MAX_DATABRICKS_SCHEMA_ALLOWLIST in backend/src/tripl/schemas/data_source.py.
// The browse is one information_schema query whatever the count, so the cap is
// about the statement's size, not its cost.
export const MAX_DATABRICKS_SCHEMA_ALLOWLIST = 50

// Mirrors _DBX_HTTP_PATH_RE on the backend: a path, not a URL.
const DATABRICKS_HTTP_PATH_RE = /^\/[A-Za-z0-9_\-./?=&]{1,499}$/

/** Why `value` is not a SQL warehouse HTTP path, or null when it is. */
export function httpPathError(value: string, requiredMessage: string): string | null {
  const trimmed = value.trim()
  if (!trimmed) return requiredMessage
  if (/^https?:\/\//i.test(trimmed)) {
    return 'Paste only the path, starting at /sql/…, not the whole URL.'
  }
  if (!DATABRICKS_HTTP_PATH_RE.test(trimmed)) {
    return 'This is not an HTTP path. It looks like /sql/1.0/warehouses/1234abcd.'
  }
  return null
}

// '' lets the backend resolve a host-aware default: 'require' for remote hosts,
// 'prefer' for localhost (dev/docker servers rarely have a certificate).
export const SSL_MODE_OPTIONS: { value: PostgresSslMode | ''; label: string }[] = [
  { value: '', label: 'Default — require for remote hosts, prefer for localhost' },
  { value: 'disable', label: 'disable — never use TLS' },
  { value: 'allow', label: 'allow — TLS only if the server insists' },
  { value: 'prefer', label: 'prefer — TLS if available, plaintext otherwise' },
  { value: 'require', label: 'require — TLS, but the certificate is not checked' },
  { value: 'verify-ca', label: 'verify-ca — TLS and the certificate must chain to the CA' },
  { value: 'verify-full', label: 'verify-full — verify-ca plus a hostname match' },
]

// ~100 GiB, mirroring DEFAULT_BIGQUERY_MAXIMUM_BYTES_BILLED on the backend.
export const DEFAULT_MAX_BILLED_BYTES_LABEL = '107374182400'

// A BigQuery schema browse costs one job per dataset, so it is hard-capped;
// the connection's own default dataset always takes the first slot, which is
// why the allowlist accepts one fewer. Mirrors MAX_SCHEMA_DATASETS /
// _MAX_DATASET_ALLOWLIST in backend/src/tripl/schemas/data_source.py. Derived
// from one number here as it is there, rather than two literals in the help
// text: the backend split them once and the write path went on accepting 50
// datasets that the browse silently truncated to 20, which is the same drift
// this help text would reintroduce if it hardcoded a bound of its own.
export const MAX_SCHEMA_DATASETS = 20
export const MAX_DATASET_ALLOWLIST = MAX_SCHEMA_DATASETS - 1

// The one native <select> look for settings forms: the data-source dialogs and
// the scan form (scanUtils re-exports it). The copies used to differ in
// background and, worse, the scan form's had no focus ring at all, so its
// selects were invisible to keyboard users.
// The ui-kit control spec: 32px, `rounded-control`, 12.5px text from
// `md` (16px on phones so iOS does not zoom), and the one `aria-invalid`
// look.
export const SELECT_CLASS =
  `flex h-8 w-full rounded-control border border-input bg-background px-2.5 py-1 ${INPUT_TEXT_CLASS} shadow-sm ` +
  `focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring ${INPUT_INVALID_CLASS}`

export const TEXTAREA_CLASS =
  `flex w-full rounded-control border border-input bg-background px-2.5 py-1.5 font-mono ${INPUT_TEXT_CLASS} shadow-sm ` +
  `focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring ${INPUT_INVALID_CLASS} ${INPUT_PLACEHOLDER_CLASS}`

export const HELP_CLASS = 'text-body-sm text-fg-tertiary'

export const ERROR_CLASS = 'text-body-sm text-destructive'

/**
 * Attributes every credential input and textarea carries.
 *
 * - No spellcheck: Chrome's enhanced spell check sends the typed text — a
 *   private key included — to a remote service.
 * - No autofill: a "Username" input followed by a password field reads as a
 *   login form, so browsers and password managers offered to save warehouse
 *   credentials as the tripl login and, worse, filled the user's tripl password
 *   into the edit dialog's empty "leave empty to keep" field, which the next
 *   unrelated save then wrote over the stored warehouse password.
 */
export const SECRET_INPUT_PROPS = {
  spellCheck: false,
  autoComplete: 'off',
  autoCorrect: 'off',
  autoCapitalize: 'off',
  'data-1p-ignore': 'true',
  'data-lpignore': 'true',
} as const

/** Same as SECRET_INPUT_PROPS, for the password field of a non-login form. */
export const PASSWORD_INPUT_PROPS = {
  ...SECRET_INPUT_PROPS,
  autoComplete: 'new-password',
} as const

type PemKind = 'certificate' | 'private key'

/**
 * Why `value` is not PEM content of the expected kind, or null when it is (or
 * is empty — emptiness is the field's own "keep / not set" state).
 *
 * Only the envelope is checked: `-----BEGIN …-----` and a matching `-----END`.
 * A partial paste or a server path ("/etc/ssl/ca.pem") is the common mistake
 * and would otherwise only surface as a failed connection much later.
 *
 * The block is searched for, not anchored: `openssl pkcs12` and
 * `openssl s_client -showcerts` output carries "Bag Attributes",
 * "subject=/issuer=" or comment lines before it, which libpq, OpenSSL and the
 * backend (it only looks for "-----BEGIN" anywhere) all skip. TRUSTED and
 * X509 certificate labels count as certificates.
 */
export function pemError(value: string, kind: PemKind): string | null {
  const trimmed = value.trim()
  if (!trimmed) return null
  const label = kind === 'certificate' ? '[A-Z0-9 ]*CERTIFICATE' : '[A-Z ]*PRIVATE KEY'
  const begin = new RegExp(`-----BEGIN (${label})-----`)
  const match = begin.exec(trimmed)
  if (!match) {
    return kind === 'certificate'
      ? 'Paste the PEM certificate itself: a -----BEGIN CERTIFICATE----- block.'
      : 'Paste the PEM private key itself: a -----BEGIN PRIVATE KEY----- block.'
  }
  if (!trimmed.includes(`-----END ${match[1]}-----`, match.index + match[0].length)) {
    return `The ${kind} is incomplete: its -----END ${match[1]}----- line is missing.`
  }
  return null
}

export type PemField = 'sslrootcert' | 'sslcert' | 'sslkey'
/**
 * Inline errors for the settings fields, by field: the Postgres PEM blocks, and
 * the Databricks HTTP path and the Snowflake warehouse (the required settings).
 */
export type PemErrors = Partial<Record<PemField | 'httpPath' | 'warehouse', string>>

/**
 * Inline errors for the settings fields: the Postgres PEM blocks, the
 * Databricks HTTP path and the Snowflake warehouse. Empty for other warehouses.
 */
export function connectionSettingsErrors(
  dbType: DbType,
  form: ConnectionSettingsForm,
  requiredMessage = REQUIRED_MESSAGE,
): PemErrors {
  if (dbType === 'databricks') {
    const httpPath = httpPathError(form.httpPath, requiredMessage)
    return httpPath ? { httpPath } : {}
  }
  if (dbType === 'snowflake') {
    const warehouse = snowflakeWarehouseError(form.warehouse, requiredMessage)
    return warehouse ? { warehouse } : {}
  }
  if (dbType !== 'postgres') return {}
  const errors: PemErrors = {}
  const root = pemError(form.sslrootcert, 'certificate')
  if (root) errors.sslrootcert = root
  const cert = pemError(form.sslcert, 'certificate')
  if (cert) errors.sslcert = cert
  const key = form.clearSslkey ? null : pemError(form.sslkey, 'private key')
  if (key) errors.sslkey = key
  return errors
}

// One field column inside a `grid-cols-N` row: label, control, and usually a
// help paragraph. `content-start` is load-bearing — without it the row height
// comes from the tallest column and grid hands the surplus to the shorter
// column's auto rows, so Username's label and input sat 12px below Password's
// purely because the Password column carried a third child.
export const FIELD_COL_CLASS = 'grid content-start gap-2'

function parseAllowlist(value: string): string[] {
  return value
    .split(/[\s,]+/)
    .map((entry) => entry.trim())
    .filter(Boolean)
}

function nullable(value: string): string | null {
  const trimmed = value.trim()
  return trimmed ? trimmed : null
}

/**
 * Convert the form into the API payload for the selected warehouse.
 *
 * Returns `undefined` for warehouses without settings so the caller can leave
 * `connection_settings` out of the request entirely. Cleared fields are sent as
 * `null` (the PATCH replaces settings wholesale). `sslkey` is only sent when the
 * operator typed a new key, or explicitly asked to remove the stored one — an
 * omitted `sslkey` keeps whatever is stored, exactly like an omitted password.
 */
export function buildConnectionSettings(
  dbType: DbType,
  form: ConnectionSettingsForm,
): ConnectionSettings | undefined {
  if (dbType === 'bigquery') {
    const maxBytes = form.maximumBytesBilled.trim()
    const datasets = parseAllowlist(form.datasetAllowlist)
    return {
      location: nullable(form.location),
      maximum_bytes_billed: maxBytes ? Number(maxBytes) : null,
      dataset_allowlist: datasets.length > 0 ? datasets : null,
    }
  }

  if (dbType === 'databricks') {
    const schemas = parseAllowlist(form.schemaAllowlist)
    return {
      http_path: form.httpPath.trim(),
      auth_type: form.authType,
      schema_name: nullable(form.schemaName),
      schema_allowlist: schemas.length > 0 ? schemas : null,
    }
  }

  if (dbType === 'snowflake') {
    const schemas = parseAllowlist(form.schemaAllowlist)
    return {
      warehouse: form.warehouse.trim(),
      auth_type: form.snowflakeAuthType,
      role: nullable(form.role),
      schema_name: nullable(form.schemaName),
      schema_allowlist: schemas.length > 0 ? schemas : null,
    }
  }

  if (dbType === 'postgres') {
    const sslkey = form.sslkey.trim()
    return {
      sslmode: form.sslmode === '' ? null : form.sslmode,
      sslrootcert: nullable(form.sslrootcert),
      sslcert: nullable(form.sslcert),
      search_path: nullable(form.searchPath),
      ...(sslkey ? { sslkey } : {}),
      ...(!sslkey && form.clearSslkey ? { sslkey: '' } : {}),
    }
  }

  return undefined
}

/** Prefill the form from a saved source (the private key is never sent back). */
export function connectionSettingsToForm(
  settings: ConnectionSettingsResponse | null | undefined,
): ConnectionSettingsForm {
  if (!settings) return EMPTY_CONNECTION_SETTINGS_FORM
  return {
    location: settings.location ?? '',
    maximumBytesBilled:
      settings.maximum_bytes_billed == null ? '' : String(settings.maximum_bytes_billed),
    datasetAllowlist: (settings.dataset_allowlist ?? []).join(', '),
    sslmode: settings.sslmode ?? '',
    sslrootcert: settings.sslrootcert ?? '',
    sslcert: settings.sslcert ?? '',
    sslkey: '',
    clearSslkey: false,
    searchPath: settings.search_path ?? '',
    httpPath: settings.http_path ?? '',
    authType:
      settings.auth_type === 'pat' || settings.auth_type === 'oauth_m2m'
        ? settings.auth_type
        : 'pat',
    schemaName: settings.schema_name ?? '',
    schemaAllowlist: (settings.schema_allowlist ?? []).join(', '),
    warehouse: settings.warehouse ?? '',
    role: settings.role ?? '',
    snowflakeAuthType: settings.auth_type === 'key_pair' ? 'key_pair' : 'password',
  }
}
