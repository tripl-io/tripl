import type {
  ConnectionSettings,
  ConnectionSettingsResponse,
  DatabricksAuthType,
  DbType,
  PostgresSslMode,
  SnowflakeAuthType,
  TrinoHttpScheme,
} from '@/types'
import { REQUIRED_MESSAGE } from '@/components/forms/validation'
import { INPUT_INVALID_CLASS, INPUT_PLACEHOLDER_CLASS, INPUT_TEXT_CLASS } from '@/components/settings/input-style'
import { countOf } from '@/lib/plural'

// Form state for the typed, per-warehouse connection settings. Kept as strings
// (what inputs produce) and converted to the API shape by
// `buildConnectionSettings`, which only ever emits the settings that apply to
// the selected warehouse — the backend rejects the rest with a 422.

/** PostgreSQL and the engines that connect over its protocol take its settings. */
export function usesPostgresSettings(dbType: DbType): boolean {
  return dbType === 'postgres' || dbType === 'greenplum' || dbType === 'redshift'
}

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
  // Trino (schemaName and schemaAllowlist above are its too)
  httpScheme: TrinoHttpScheme
  // Athena (schemaAllowlist above is its too)
  workGroup: string
  s3OutputLocation: string
  catalogName: string
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
  httpScheme: 'https',
  workGroup: '',
  s3OutputLocation: '',
  catalogName: '',
}

export const TRINO_SCHEME_OPTIONS: { value: TrinoHttpScheme; label: string }[] = [
  { value: 'https', label: 'HTTPS (password sign-in)' },
  { value: 'http', label: 'HTTP (no authentication; local or in-cluster only)' },
]

// How many schemas one schema allowlist may name: Databricks, Snowflake, Trino
// and Athena alike. Their browse is one information_schema query whatever the
// count, so the cap is about the statement's size, not its cost. Mirrors
// MAX_SCHEMA_ALLOWLIST in backend/src/tripl/schemas/connection_settings_base.py.
export const MAX_SCHEMA_ALLOWLIST = 50

// Mirrors _S3_URI_RE on the backend: where Athena writes query results.
const S3_URI_RE = /^s3:\/\/[a-z0-9][a-z0-9.-]{1,61}[a-z0-9](\/[^\s'"\\]{0,900})?$/

/** Why `value` is not an S3 output location, or null when it is (or is empty). */
export function s3OutputLocationError(value: string): string | null {
  const trimmed = value.trim()
  if (!trimmed) return null
  if (!S3_URI_RE.test(trimmed)) {
    return 'Use an S3 location, like s3://my-bucket/athena-results/.'
  }
  return null
}

export const DATABRICKS_AUTH_OPTIONS: { value: DatabricksAuthType; label: string }[] = [
  { value: 'pat', label: 'Access token (personal or service principal)' },
  { value: 'oauth_m2m', label: 'OAuth machine-to-machine (service principal)' },
]

export const SNOWFLAKE_AUTH_OPTIONS: { value: SnowflakeAuthType; label: string }[] = [
  { value: 'password', label: 'Password' },
  { value: 'key_pair', label: 'Key pair (private key in the secret field)' },
]

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

/**
 * Why a schema or dataset name cannot be saved, or null. Only the commonest
 * mistake is caught here, a name qualified by what holds it (`hive.events`,
 * `my-project.analytics`): no warehouse's names take a dot, and the server
 * checks the rest.
 */
export function qualifiedNameError(value: string): string | null {
  const trimmed = value.trim()
  const dot = trimmed.lastIndexOf('.')
  if (dot < 0) return null
  const name = trimmed.slice(dot + 1)
  return name ? `Use the name alone, like ${name}, not ${trimmed}.` : 'Use the name alone, without a dot.'
}

/**
 * Why a comma-separated allowlist cannot be saved, or null: a qualified entry,
 * or more distinct entries than `limit` (the server drops repeats first too).
 */
export function allowlistError(value: string, limit: number, noun: [string, string]): string | null {
  const entries = parseAllowlist(value)
  for (const entry of entries) {
    const qualified = qualifiedNameError(entry)
    if (qualified) return qualified
  }
  const count = new Set(entries).size
  return count > limit ? `At most ${countOf(limit, ...noun)}: this lists ${count}.` : null
}

export type PemField = 'sslrootcert' | 'sslcert' | 'sslkey'
export type SettingsField =
  | PemField
  | 'httpPath'
  | 'warehouse'
  | 's3OutputLocation'
  | 'schemaName'
  | 'schemaAllowlist'
  | 'datasetAllowlist'
/** Inline errors for the settings fields, by field. */
export type SettingsErrors = Partial<Record<SettingsField, string>>

// The settings a warehouse cannot connect without.
const REQUIRED_SETTINGS: ReadonlySet<SettingsField> = new Set<SettingsField>(['httpPath', 'warehouse'])

// The warehouses with a default schema, and those with a schema allowlist.
const SCHEMA_NAME_TYPES: ReadonlySet<DbType> = new Set<DbType>(['databricks', 'snowflake', 'trino'])
const SCHEMA_ALLOWLIST_TYPES: ReadonlySet<DbType> = new Set<DbType>([
  'databricks',
  'snowflake',
  'trino',
  'athena',
])

/**
 * Inline errors for the settings fields that apply to `dbType`: the Postgres
 * PEM blocks, the Databricks HTTP path, the Snowflake warehouse, the Athena
 * result location, and the schema and dataset names and allowlists.
 */
export function connectionSettingsErrors(
  dbType: DbType,
  form: ConnectionSettingsForm,
  requiredMessage = REQUIRED_MESSAGE,
): SettingsErrors {
  const errors: SettingsErrors = {}
  const flag = (field: SettingsField, message: string | null) => {
    if (message) errors[field] = message
  }
  if (dbType === 'bigquery') {
    flag(
      'datasetAllowlist',
      allowlistError(form.datasetAllowlist, MAX_DATASET_ALLOWLIST, ['dataset', 'datasets']),
    )
  } else if (dbType === 'databricks') {
    flag('httpPath', httpPathError(form.httpPath, requiredMessage))
  } else if (dbType === 'snowflake') {
    flag('warehouse', snowflakeWarehouseError(form.warehouse, requiredMessage))
  } else if (dbType === 'athena') {
    flag('s3OutputLocation', s3OutputLocationError(form.s3OutputLocation))
  } else if (usesPostgresSettings(dbType)) {
    flag('sslrootcert', pemError(form.sslrootcert, 'certificate'))
    flag('sslcert', pemError(form.sslcert, 'certificate'))
    if (!form.clearSslkey) flag('sslkey', pemError(form.sslkey, 'private key'))
  }
  if (SCHEMA_NAME_TYPES.has(dbType)) flag('schemaName', qualifiedNameError(form.schemaName))
  if (SCHEMA_ALLOWLIST_TYPES.has(dbType)) {
    const noun: [string, string] = dbType === 'athena' ? ['database', 'databases'] : ['schema', 'schemas']
    flag('schemaAllowlist', allowlistError(form.schemaAllowlist, MAX_SCHEMA_ALLOWLIST, noun))
  }
  return errors
}

/**
 * `connectionSettingsErrors` for a form opened over `baseline` (the stored
 * settings on edit, the empty form on create). A field is only checked once it
 * differs from the baseline, so a value saved before a check existed cannot
 * block an unrelated edit. The required settings are checked whatever is
 * stored: without them there is no warehouse to connect to.
 */
export function editedSettingsErrors(
  dbType: DbType,
  form: ConnectionSettingsForm,
  baseline: ConnectionSettingsForm,
  requiredMessage = REQUIRED_MESSAGE,
): SettingsErrors {
  const errors: SettingsErrors = {}
  const all = connectionSettingsErrors(dbType, form, requiredMessage)
  for (const [field, error] of Object.entries(all) as [SettingsField, string][]) {
    if (REQUIRED_SETTINGS.has(field) || form[field] !== baseline[field]) errors[field] = error
  }
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

/** An allowlist as the API takes it: its names, or null for none. */
function allowlistSetting(value: string): string[] | null {
  const names = parseAllowlist(value)
  return names.length > 0 ? names : null
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
    return {
      location: nullable(form.location),
      maximum_bytes_billed: maxBytes ? Number(maxBytes) : null,
      dataset_allowlist: allowlistSetting(form.datasetAllowlist),
    }
  }

  if (dbType === 'databricks') {
    return {
      http_path: form.httpPath.trim(),
      auth_type: form.authType,
      schema_name: nullable(form.schemaName),
      schema_allowlist: allowlistSetting(form.schemaAllowlist),
    }
  }

  if (dbType === 'snowflake') {
    return {
      warehouse: form.warehouse.trim(),
      auth_type: form.snowflakeAuthType,
      role: nullable(form.role),
      schema_name: nullable(form.schemaName),
      schema_allowlist: allowlistSetting(form.schemaAllowlist),
    }
  }

  if (dbType === 'trino') {
    return {
      http_scheme: form.httpScheme,
      schema_name: nullable(form.schemaName),
      schema_allowlist: allowlistSetting(form.schemaAllowlist),
    }
  }

  if (dbType === 'athena') {
    return {
      work_group: nullable(form.workGroup),
      s3_output_location: nullable(form.s3OutputLocation),
      catalog_name: nullable(form.catalogName),
      schema_allowlist: allowlistSetting(form.schemaAllowlist),
    }
  }

  if (usesPostgresSettings(dbType)) {
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
    httpScheme: settings.http_scheme === 'http' ? 'http' : 'https',
    workGroup: settings.work_group ?? '',
    s3OutputLocation: settings.s3_output_location ?? '',
    catalogName: settings.catalog_name ?? '',
  }
}
