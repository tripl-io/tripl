import {
  bigquery,
  clickhouse,
  formatDialect,
  postgresql,
  redshift,
  snowflake,
  spark,
  sql,
  type DialectOptions,
  type SqlLanguage,
} from 'sql-formatter'

/**
 * The SQL formatter, restricted to the dialects `formatLanguage` can return.
 *
 * `format(query, { language })` reaches every dialect through a lookup table,
 * so importing it bundled all eighteen of them; `formatDialect` with the six
 * dialect objects lets the rest tree-shake away. The editor also imports this
 * module on the first Format click rather than statically, so a page that only
 * shows SQL never downloads the formatter at all.
 */
const DIALECTS: Partial<Record<SqlLanguage, DialectOptions>> = {
  postgresql,
  clickhouse,
  bigquery,
  // Databricks SQL is Spark SQL with extensions; sql-formatter's Spark dialect
  // is the nearest it ships.
  spark,
  snowflake,
  redshift,
  sql,
}

export function formatSql(query: string, language: SqlLanguage): string {
  return formatDialect(query, { dialect: DIALECTS[language] ?? sql })
}
