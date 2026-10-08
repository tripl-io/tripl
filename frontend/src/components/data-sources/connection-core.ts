import type { DataSource, DbType, JsonPathDiscovery } from '@/types'

/**
 * Form state for the *core* connection fields — the ones that live on the data
 * source row itself (host/port/database/username/password/timeout), as opposed
 * to the typed per-warehouse `connection_settings` handled by
 * `./connection-settings`.
 *
 * What a field *means* depends on the warehouse:
 *   - `host`         → the GCP project id for BigQuery, a hostname elsewhere
 *   - `databaseName` → the default dataset for BigQuery, a database elsewhere
 *   - `secret`       → the service-account JSON for BigQuery, a password elsewhere
 *   - `port`/`username` → meaningless for BigQuery (`BigQueryAdapter.__init__`
 *     deletes both), so they are neither shown nor sent for it.
 *   - Databricks: `host` is the workspace hostname, `databaseName` the catalog,
 *     `secret` an access token (or an OAuth secret) and `username` a service
 *     principal's OAuth client ID. The port is always 443 and is not shown.
 *   - Snowflake: `host` is the account identifier, `databaseName` the
 *     database, `username` the user and `secret` its password or, for key-pair
 *     sign-in, its PEM private key. The port is always 443 and is not shown.
 *   - Trino: `host`/`port` reach the coordinator, `databaseName` is the
 *     catalog, and the password is optional (an unauthenticated coordinator).
 *   - Athena: `host` is the AWS region, `databaseName` the default Glue
 *     database, `username` the access key ID and `secret` the secret access
 *     key. The port is always 443 and is not shown.
 *
 * `secret` is write-only. The API never returns a password or a service-account
 * key (only the `password_set` boolean), so it always starts empty on edit and
 * an untouched field keeps whatever is stored.
 */
export interface ConnectionCoreForm {
  host: string
  port: number
  databaseName: string
  username: string
  secret: string
  timeoutSeconds: string
  jsonPathDiscovery: JsonPathDiscovery
}

export const EMPTY_CONNECTION_CORE_FORM: ConnectionCoreForm = {
  host: '',
  port: 8123,
  databaseName: '',
  username: '',
  secret: '',
  timeoutSeconds: '',
  jsonPathDiscovery: 'dynamic',
}

/**
 * Prefill the edit form from a saved source. The secret is deliberately left
 * empty: the GET never returns it, so echoing anything here would be a lie (and
 * would risk sending a placeholder back as a real credential).
 */
export function dataSourceToCoreForm(ds: DataSource): ConnectionCoreForm {
  return {
    host: ds.host,
    port: ds.port,
    databaseName: ds.database_name,
    username: ds.username,
    secret: '',
    timeoutSeconds: ds.timeout_seconds == null ? '' : String(ds.timeout_seconds),
    jsonPathDiscovery: ds.json_path_discovery ?? 'dynamic',
  }
}

export function parseTimeoutSeconds(value: string): number | null {
  const trimmed = value.trim()
  if (!trimmed) return null
  const parsed = Number(trimmed)
  return Number.isFinite(parsed) ? parsed : null
}

/** Databricks SQL warehouses are reached over HTTPS only. */
export const DATABRICKS_PORT = 443

/** The warehouses reached over HTTPS on 443 only, with no port box. */
export function isHttpsOnly(dbType: DbType): boolean {
  return dbType === 'databricks' || dbType === 'snowflake' || dbType === 'athena'
}

interface CoreCreatePayload {
  host: string
  port: number
  database_name: string
  username: string
  password: string
  timeout_seconds: number | null
  json_path_discovery?: JsonPathDiscovery
}

interface CoreUpdatePayload {
  host: string
  port?: number
  database_name: string
  username?: string
  password?: string
  timeout_seconds: number | null
  json_path_discovery?: JsonPathDiscovery
}

/**
 * The POST body for the core fields. BigQuery still sends the (unused) port and
 * username because `DataSourceCreate` defaults them to 8123/"" anyway — sending
 * the defaults keeps the create contract exactly as it was.
 */
export function buildCoreCreatePayload(
  dbType: DbType,
  form: ConnectionCoreForm,
): CoreCreatePayload {
  return {
    host: form.host,
    port: isHttpsOnly(dbType) ? DATABRICKS_PORT : form.port,
    database_name: form.databaseName,
    username: form.username,
    password: form.secret,
    // Every warehouse honours the timeout, BigQuery included.
    timeout_seconds: parseTimeoutSeconds(form.timeoutSeconds),
    ...(dbType === 'clickhouse' ? { json_path_discovery: form.jsonPathDiscovery } : {}),
  }
}

/**
 * The PATCH body for the core fields.
 *
 * BigQuery omits `port` and `username` entirely — the adapter deletes them, so
 * the edit dialog does not show them and must not write them back. The secret is
 * only sent when the operator actually typed one; omitting it keeps the stored
 * password / service-account key, exactly like an omitted `sslkey`.
 */
export function buildCoreUpdatePayload(
  dbType: DbType,
  form: ConnectionCoreForm,
): CoreUpdatePayload {
  const timeout_seconds = parseTimeoutSeconds(form.timeoutSeconds)

  if (dbType === 'bigquery') {
    return {
      host: form.host,
      database_name: form.databaseName,
      ...(form.secret ? { password: form.secret } : {}),
      timeout_seconds,
    }
  }

  return {
    host: form.host,
    port: isHttpsOnly(dbType) ? DATABRICKS_PORT : form.port,
    database_name: form.databaseName,
    username: form.username,
    ...(form.secret ? { password: form.secret } : {}),
    timeout_seconds,
    ...(dbType === 'clickhouse' ? { json_path_discovery: form.jsonPathDiscovery } : {}),
  }
}

/**
 * Why `value` is not a usable service-account key file, or null when it is (or
 * is empty — on edit that means "keep the stored key", and on create
 * `connectionCoreMissing` catches it).
 *
 * A partial paste used to be accepted at create time and only fail later, at
 * connect or test time, in the backend's `json.loads`.
 */
export function serviceAccountKeyError(value: string): string | null {
  const trimmed = value.trim()
  if (!trimmed) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(trimmed)
  } catch {
    return 'This is not valid JSON. Paste the whole key file, from the opening { to the closing }.'
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return 'The key file must be a JSON object.'
  }
  const record = parsed as Record<string, unknown>
  if (record.type !== 'service_account') {
    return 'This is not a service-account key: its "type" must be "service_account".'
  }
  return null
}

/** The core fields a connection cannot be tested or saved without. */
export type CoreMissing = Partial<Record<'host' | 'port' | 'databaseName' | 'secret', string>>

/**
 * Which required core fields are empty, each mapped to the inline message
 * under it. The dialogs validate on submit with `noValidate`
 * instead of the browser's bubble, which flagged the first empty field only,
 * and Test connection used to send the empty draft and come back with the
 * backend's "host: String should have at least 1 character".
 *
 * BigQuery and Databricks have no port box, and their key or token is required
 * only on create: on edit an empty field keeps the stored one.
 */
export function connectionCoreMissing(
  dbType: DbType,
  form: ConnectionCoreForm,
  mode: 'create' | 'edit',
  message: string,
): CoreMissing {
  const missing: CoreMissing = {}
  if (!form.host.trim()) missing.host = message
  if (!form.databaseName.trim()) missing.databaseName = message
  if (dbType === 'bigquery' || isHttpsOnly(dbType)) {
    // None has a port box; all need their credential up front.
    if (mode === 'create' && !form.secret.trim()) missing.secret = message
  } else if (!form.port) {
    missing.port = message
  }
  return missing
}

// The API's field name for each core control. `password` carries the BigQuery
// service-account key too.
const API_FIELD_TO_CORE: Readonly<Record<string, keyof CoreMissing>> = {
  host: 'host',
  port: 'port',
  database_name: 'databaseName',
  password: 'secret',
}

// Pydantic's "empty string" family, reworded as the inline required message.
const EMPTY_VALUE_TYPES: ReadonlySet<string> = new Set(['string_too_short', 'missing'])

/** A server refusal split into the parts that name a core field and the rest. */
export interface ServerCoreErrors {
  fields: CoreMissing
  /** Anything no core control can show, in the server's words; null when none. */
  rest: string | null
}

/**
 * Map a 422 from create or test onto the core controls. The client
 * catches empty required fields itself (`connectionCoreMissing`); this covers
 * whatever it misses, so a refusal lands under the control it is about
 * ("Required" under Database) instead of as "database_name: String should have
 * at least 1 character" at the foot of the dialog.
 *
 * Accepts any thrown value. Only an error carrying FastAPI's structured
 * `fields` is split; anything else is returned whole as `rest`.
 */
export function serverCoreErrors(
  error: unknown,
  dbType: DbType,
  requiredMessage: string,
): ServerCoreErrors {
  if (error == null) return { fields: {}, rest: null }
  const message = error instanceof Error ? error.message : String(error)
  const items = (error as { fields?: { loc: (string | number)[]; msg: string; type: string }[] })
    .fields
  if (!Array.isArray(items) || items.length === 0) return { fields: {}, rest: message || null }
  const fields: CoreMissing = {}
  const rest: string[] = []
  for (const item of items) {
    const last = item.loc[item.loc.length - 1]
    const mapped = typeof last === 'string' ? API_FIELD_TO_CORE[last] : undefined
    // Only controls the type actually shows: BigQuery and Databricks have no
    // port, and only their key or token field (not the password box) renders an
    // inline error.
    const shown =
      dbType === 'bigquery' || isHttpsOnly(dbType) ? mapped !== 'port' : mapped !== 'secret'
    const key = shown ? mapped : undefined
    if (key && !fields[key]) {
      fields[key] = EMPTY_VALUE_TYPES.has(item.type) ? requiredMessage : item.msg
    } else if (!key) {
      const path = item.loc.filter((seg) => seg !== 'body' && seg !== 'query').join('.')
      rest.push(path ? `${path}: ${item.msg}` : item.msg)
    }
  }
  return { fields, rest: rest.length > 0 ? rest.join('; ') : null }
}

/** Inline error for the core secret field, or null. Only BigQuery's is checked. */
export function connectionCoreSecretError(dbType: DbType, form: ConnectionCoreForm): string | null {
  return dbType === 'bigquery' ? serviceAccountKeyError(form.secret) : null
}

/**
 * Whether saving `form` over `ds` changes how tripl connects, so a fresh
 * connection test is worth running afterwards. A rename or a timeout
 * change is not a connection change.
 */
export function coreConnectionChanged(ds: DataSource, form: ConnectionCoreForm): boolean {
  if (form.secret) return true
  if (form.host !== ds.host || form.databaseName !== ds.database_name) return true
  if (ds.db_type === 'bigquery') return false
  return form.port !== ds.port || form.username !== ds.username
}
