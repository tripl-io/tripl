import { PostgreSQL, SQLDialect, StandardSQL } from '@codemirror/lang-sql'
import type { SqlLanguage } from 'sql-formatter'
import type { DbType } from '@/types/dataSources'

/**
 * Per-engine CodeMirror SQL dialects driving keyword highlighting and the
 * autocomplete word list. lang-sql ships no ClickHouse/BigQuery/Databricks/Snowflake dialect, so we
 * define lightweight ones from curated keyword/function/type lists.
 *
 * Casing rule (lang-sql stores completion labels verbatim but tokenises
 * lower-cased): ClickHouse function/type names are case-SENSITIVE, so they are
 * listed in their exact camelCase form — completion then inserts them verbatim
 * (their syntax colouring is skipped, an acceptable trade for correct text).
 * BigQuery is case-INSENSITIVE, so its functions/types are listed lower-case,
 * which both inserts validly and still colours; so are Databricks (Spark SQL)
 * and Snowflake.
 * Postgres uses lang-sql's own,
 * already-complete dialect.
 */

// Core ANSI clauses shared by every engine; lower-case so the tokeniser colours
// them and completion offers the familiar lower-case form (SQL keywords are
// case-insensitive everywhere we support).
const COMMON_KEYWORDS =
  'with select distinct from where group by having qualify window over partition ' +
  'order asc desc limit offset as join inner left right full outer cross lateral ' +
  'on using union all except intersect case when then else end and or not in is ' +
  'null like ilike between exists cast filter'

const CLICKHOUSE_FUNCTIONS =
  'count countIf countDistinct uniq uniqExact uniqCombined sum sumIf avg avgIf min max ' +
  'any anyLast argMin argMax median quantile quantiles groupArray groupUniqArray ' +
  'toDate toDateTime toDateTime64 toStartOfMinute toStartOfFiveMinutes toStartOfHour ' +
  'toStartOfDay toStartOfWeek toStartOfMonth toStartOfInterval now today yesterday ' +
  'dateDiff dateAdd toUnixTimestamp if multiIf coalesce ifNull nullIf isNull isNotNull ' +
  'toString toInt64 toUInt64 toFloat64 lower upper length substring splitByChar ' +
  'arrayJoin has empty notEmpty round floor ceil abs'
const CLICKHOUSE_TYPES =
  'UInt8 UInt16 UInt32 UInt64 Int8 Int16 Int32 Int64 Float32 Float64 Decimal String ' +
  'FixedString Date Date32 DateTime DateTime64 Bool UUID Array Nullable LowCardinality Map Tuple'

const ClickHouseDialect = SQLDialect.define({
  keywords: `${COMMON_KEYWORDS} prewhere final sample settings format array`,
  builtin: CLICKHOUSE_FUNCTIONS,
  types: CLICKHOUSE_TYPES,
  identifierQuotes: '`"',
  backslashEscapes: true,
})

const BIGQUERY_FUNCTIONS =
  'count countif sum avg min max approx_count_distinct array_agg string_agg logical_and ' +
  'logical_or timestamp_trunc datetime_trunc date_trunc time_trunc extract date datetime ' +
  'time timestamp current_date current_timestamp date_add date_sub date_diff timestamp_add ' +
  'timestamp_sub timestamp_diff format_timestamp parse_timestamp safe_cast coalesce ifnull ' +
  'nullif if least greatest concat lower upper substr split trim length row_number rank ' +
  'dense_rank lag lead first_value last_value ntile percentile_cont generate_array ' +
  'generate_date_array unnest'
const BIGQUERY_TYPES =
  'int64 numeric bignumeric float64 bool string bytes date datetime time timestamp interval ' +
  'array struct geography json'

const BigQueryDialect = SQLDialect.define({
  keywords: `${COMMON_KEYWORDS} qualify unnest struct safe`,
  builtin: BIGQUERY_FUNCTIONS,
  types: BIGQUERY_TYPES,
  identifierQuotes: '`',
})

const DATABRICKS_FUNCTIONS =
  'count count_if sum avg min max approx_count_distinct collect_list collect_set ' +
  'array_agg string_agg any_value bool_and bool_or date_trunc date_add date_sub datediff ' +
  'timestampadd timestampdiff from_utc_timestamp to_utc_timestamp to_timestamp to_date ' +
  'unix_seconds timestamp_seconds current_date current_timestamp extract try_cast cast ' +
  'coalesce ifnull nullif nvl if lower upper length substring split regexp_extract rlike ' +
  'get_json_object from_json to_json parse_json try_parse_json schema_of_variant ' +
  'variant_get try_variant_get map_keys element_at try_element_at explode size ' +
  'row_number rank dense_rank lag lead first_value last_value ntile percentile_approx ' +
  'round floor ceil abs'
const DATABRICKS_TYPES =
  'tinyint smallint int bigint float double decimal boolean string binary date timestamp ' +
  'timestamp_ntz interval array map struct variant'

const DatabricksDialect = SQLDialect.define({
  keywords: `${COMMON_KEYWORDS} qualify rlike regexp lateral view pivot unpivot tablesample`,
  builtin: DATABRICKS_FUNCTIONS,
  types: DATABRICKS_TYPES,
  identifierQuotes: '`',
  backslashEscapes: true,
})

const SNOWFLAKE_FUNCTIONS =
  'count count_if sum avg min max approx_count_distinct array_agg listagg any_value ' +
  'booland_agg boolor_agg date_trunc time_slice dateadd datediff timestampadd ' +
  'timestampdiff date_part convert_timezone to_timestamp to_timestamp_ntz to_timestamp_tz ' +
  'to_timestamp_ltz to_date current_date current_timestamp extract try_cast cast ' +
  'try_to_double try_to_number coalesce ifnull nullif nvl iff lower upper length substr ' +
  'split regexp_like regexp_instr regexp_substr parse_json try_parse_json to_json ' +
  'object_keys get get_path flatten is_object is_array typeof array_size array_sort ' +
  'array_construct row_number rank dense_rank lag lead first_value last_value ntile ' +
  'percentile_cont round floor ceil abs'
const SNOWFLAKE_TYPES =
  'number decimal numeric int integer bigint smallint float double real boolean string ' +
  'varchar text binary date time timestamp timestamp_ntz timestamp_ltz timestamp_tz ' +
  'variant object array'

const SnowflakeDialect = SQLDialect.define({
  keywords: `${COMMON_KEYWORDS} qualify lateral flatten pivot unpivot sample`,
  builtin: SNOWFLAKE_FUNCTIONS,
  types: SNOWFLAKE_TYPES,
  identifierQuotes: '"',
  backslashEscapes: true,
})

const HIGHLIGHT_DIALECT: Record<DbType, SQLDialect> = {
  postgres: PostgreSQL,
  clickhouse: ClickHouseDialect,
  bigquery: BigQueryDialect,
  databricks: DatabricksDialect,
  snowflake: SnowflakeDialect,
  // The local demo synthetic source mimics ClickHouse semantics, so reuse its
  // dialect for highlighting/autocomplete of the (rarely-edited) demo SQL.
  synthetic: ClickHouseDialect,
}

/** CodeMirror dialect for the engine; StandardSQL when none is selected yet. */
export function highlightDialect(db: DbType | undefined): SQLDialect {
  return db ? HIGHLIGHT_DIALECT[db] : StandardSQL
}

const FORMAT_LANGUAGE: Record<DbType, SqlLanguage> = {
  postgres: 'postgresql',
  clickhouse: 'clickhouse',
  bigquery: 'bigquery',
  databricks: 'spark',
  snowflake: 'snowflake',
  synthetic: 'clickhouse',
}

/** sql-formatter language for the engine; generic 'sql' when none is selected. */
export function formatLanguage(db: DbType | undefined): SqlLanguage {
  return db ? FORMAT_LANGUAGE[db] : 'sql'
}
