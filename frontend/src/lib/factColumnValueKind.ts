export type FactColumnValueKind = 'number' | 'boolean' | 'string'

/**
 * Scalar kind of a fact-table column, for condition serialization and display.
 *
 * `FactTableColumn.type` is NOT a warehouse type name: fact-table introspection
 * already buckets every ClickHouse / BigQuery / Databricks / PostgreSQL type into one of
 * `number` | `string` | `bool` | `timestamp` before it reaches the API (see
 * `fact_table_introspection_service`). The raw adapter type travels separately
 * as `native_type`, which only the backend's type-directed SQL builders read.
 * So this maps those four buckets and nothing else — anything unrecognized
 * (`timestamp` included) serializes as a quoted string.
 */
export function factColumnValueKind(
  columnType: string | null | undefined,
): FactColumnValueKind {
  switch (columnType) {
    case 'number':
      return 'number'
    case 'bool':
      return 'boolean'
    default:
      return 'string'
  }
}
