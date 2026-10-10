// 'synthetic' is a local, in-memory demo warehouse. It is a valid db_type on the
// wire (demo sources report it) but is intentionally NOT user-selectable, so it
// is excluded from DB_TYPE_OPTIONS below.
export type DbType =
  | 'clickhouse'
  | 'postgres'
  | 'bigquery'
  | 'databricks'
  | 'snowflake'
  | 'greenplum'
  | 'redshift'
  | 'trino'
  | 'athena'
  | 'synthetic'

export interface DbTypeOption {
  value: DbType
  /** The warehouse's name, never qualified: names and placeholders build on it. */
  label: string
  defaultPort: number
  /**
   * Not yet verified against a live warehouse: its live conformance suite has
   * not passed. The type picker and the source's card say so. Removed once the
   * suite passes.
   */
  preview?: boolean
}

export const DB_TYPE_OPTIONS: DbTypeOption[] = [
  { value: 'clickhouse', label: 'ClickHouse', defaultPort: 8123 },
  { value: 'postgres', label: 'PostgreSQL', defaultPort: 5432 },
  { value: 'bigquery', label: 'BigQuery', defaultPort: 0 },
  // Always HTTPS on 443; the form does not show a port for it.
  { value: 'databricks', label: 'Databricks', defaultPort: 443 },
  // Always HTTPS on 443, like Databricks.
  { value: 'snowflake', label: 'Snowflake', defaultPort: 443, preview: true },
  // Greenplum and Redshift speak the PostgreSQL protocol and take its settings.
  { value: 'greenplum', label: 'Greenplum', defaultPort: 5432 },
  { value: 'redshift', label: 'Amazon Redshift', defaultPort: 5439, preview: true },
  // Trino / Starburst coordinator: HTTPS on 443 by default (8080 for a local,
  // unauthenticated one over HTTP).
  { value: 'trino', label: 'Trino / Starburst', defaultPort: 443, preview: true },
  // The Athena API is HTTPS on 443; the host field holds the AWS region.
  { value: 'athena', label: 'Amazon Athena', defaultPort: 443, preview: true },
]

/** The warehouse's display name ("ClickHouse"), or the raw type for one the list lacks. */
export function dbTypeLabel(dbType: DbType): string {
  return DB_TYPE_OPTIONS.find(option => option.value === dbType)?.label ?? dbType
}

/** Whether this warehouse's connector is still in preview (see {@link DbTypeOption.preview}). */
export function isPreviewDbType(dbType: DbType): boolean {
  return DB_TYPE_OPTIONS.some(option => option.value === dbType && option.preview === true)
}

export type DataSourceTestStatus = 'success' | 'failed'

// ClickHouse-only knob for the JSON path discovery preview step:
//   'dynamic' (effective default when null) → JSONDynamicPaths, only the
//             important typed sub-paths (faster, fewer paths).
//   'all'     → JSONAllPaths, every path including shared-data ones
//             (exhaustive but slower on wide JSON columns).
// Ignored by Postgres/BigQuery and does not affect scan-time value extraction.
export type JsonPathDiscovery = 'all' | 'dynamic'

// psql's sslmode ladder. The backend default for PostgreSQL is 'prefer'.
export type PostgresSslMode =
  | 'disable'
  | 'allow'
  | 'prefer'
  | 'require'
  | 'verify-ca'
  | 'verify-full'

// Typed, per-warehouse connection settings. The backend validates the payload
// against the model for the source's db_type: a setting that belongs to another
// warehouse (or to none) is a 422, not a silently dropped key.
export interface BigQueryConnectionSettings {
  location?: string | null
  maximum_bytes_billed?: number | null
  dataset_allowlist?: string[] | null
}

export interface PostgresConnectionSettings {
  sslmode?: PostgresSslMode | null
  sslrootcert?: string | null
  sslcert?: string | null
  // Write-only: PEM content of the client private key. Never returned by a GET.
  sslkey?: string | null
  search_path?: string | null
}

// 'pat' (default): a personal or service-principal access token in the password.
// 'oauth_m2m': a service principal's OAuth client ID (username) and secret (password).
export type DatabricksAuthType = 'pat' | 'oauth_m2m'

export interface DatabricksConnectionSettings {
  // Required: the SQL warehouse's HTTP path, e.g. /sql/1.0/warehouses/1234abcd.
  http_path: string
  auth_type?: DatabricksAuthType | null
  schema_name?: string | null
  schema_allowlist?: string[] | null
}

// 'password' (default): the user's password. 'key_pair': the user's PEM private
// key in the password slot.
export type SnowflakeAuthType = 'password' | 'key_pair'

export interface SnowflakeConnectionSettings {
  // Required: the virtual warehouse queries run on, e.g. COMPUTE_WH.
  warehouse: string
  auth_type?: SnowflakeAuthType | null
  role?: string | null
  schema_name?: string | null
  schema_allowlist?: string[] | null
}

// 'https' (default) or 'http' for an unauthenticated local coordinator.
export type TrinoHttpScheme = 'https' | 'http'

export interface TrinoConnectionSettings {
  http_scheme?: TrinoHttpScheme | null
  schema_name?: string | null
  schema_allowlist?: string[] | null
}

export interface AthenaConnectionSettings {
  // Unset: the account's `primary` workgroup.
  work_group?: string | null
  // Unset: the workgroup's own result location.
  s3_output_location?: string | null
  // Unset: AwsDataCatalog.
  catalog_name?: string | null
  schema_allowlist?: string[] | null
}

// ClickHouse and the synthetic warehouse have no connection settings of their own.
export type ConnectionSettings =
  | BigQueryConnectionSettings
  | PostgresConnectionSettings
  | DatabricksConnectionSettings
  | SnowflakeConnectionSettings
  | TrinoConnectionSettings
  | AthenaConnectionSettings

// Read side: the union flattened, with the private key replaced by a boolean.
// Only the fields applicable to the source's db_type are ever populated.
export interface ConnectionSettingsResponse {
  location: string | null
  maximum_bytes_billed: number | null
  dataset_allowlist: string[] | null
  sslmode: PostgresSslMode | null
  sslrootcert: string | null
  sslcert: string | null
  search_path: string | null
  sslkey_set: boolean
  // Databricks (absent from older servers' responses). `auth_type`,
  // `schema_name` and `schema_allowlist` are Snowflake's too.
  http_path?: string | null
  auth_type?: DatabricksAuthType | SnowflakeAuthType | null
  schema_name?: string | null
  schema_allowlist?: string[] | null
  // Snowflake
  warehouse?: string | null
  role?: string | null
  // Trino (`schema_name` and `schema_allowlist` above are its too)
  http_scheme?: TrinoHttpScheme | null
  // Athena (`schema_allowlist` above is its too)
  work_group?: string | null
  s3_output_location?: string | null
  catalog_name?: string | null
}

export interface DataSource {
  id: string
  // Ownership scope: null = workspace-global (shared across projects); a non-null
  // owner scopes the source to one project (e.g. a demo's synthetic warehouse).
  // Lets project surfaces filter out sources owned by other projects.
  project_id?: string | null
  name: string
  db_type: DbType
  is_synthetic: boolean
  host: string
  port: number
  database_name: string
  username: string
  password_set: boolean
  timeout_seconds: number | null
  json_path_discovery: JsonPathDiscovery | null
  connection_settings: ConnectionSettingsResponse
  last_test_at: string | null
  last_test_status: DataSourceTestStatus | null
  last_test_message: string | null
  created_at: string
  updated_at: string
  // What depends on the source across the workspace, deleted with it.
  // Always sent; optional so fixtures written before them still type.
  scan_count?: number
  scan_run_count?: number
  // The scans behind `scan_count`, for the card's "Used by" links: capped by
  // the server (`scan_count` keeps the total), and only scans in projects the
  // reader can read.
  scans?: DataSourceScanRef[]
}

/** One scan reading a data source. */
export interface DataSourceScanRef {
  id: string
  name: string
  project_slug: string
  project_name: string
}

/** POST /data-sources/test: a probe of an unsaved config, which stores nothing. */
export interface DataSourceDraftTestResult {
  success: boolean
  message: string
  tested_at: string
}

export interface DataSourceTestResult {
  success: boolean
  message: string
  tested_at: string
  data_source: DataSource
}
