// 'synthetic' is a local, in-memory demo warehouse. It is a valid db_type on the
// wire (demo sources report it) but is intentionally NOT user-selectable, so it
// is excluded from DB_TYPE_OPTIONS below.
export type DbType = 'clickhouse' | 'postgres' | 'bigquery' | 'synthetic'

export const DB_TYPE_OPTIONS: { value: DbType; label: string; defaultPort: number }[] = [
  { value: 'clickhouse', label: 'ClickHouse', defaultPort: 8123 },
  { value: 'postgres', label: 'PostgreSQL', defaultPort: 5432 },
  { value: 'bigquery', label: 'BigQuery', defaultPort: 0 },
]

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

// ClickHouse and the synthetic warehouse have no connection settings of their own.
export type ConnectionSettings = BigQueryConnectionSettings | PostgresConnectionSettings

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
