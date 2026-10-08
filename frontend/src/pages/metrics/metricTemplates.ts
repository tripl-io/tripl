import {
  Activity,
  AlertTriangle,
  ArrowRightLeft,
  DollarSign,
  ShoppingCart,
  Users,
  type LucideIcon,
} from 'lucide-react'
import type {
  MetricAggregation,
  MetricComposition,
  MetricKind,
  MetricScanInterval,
} from '@/types'
import type { DbType } from '@/types/dataSources'

// The editable-field seed a template applies to the create form. Only fields a
// template can sensibly prefill are here: kind + kind-specific shape + the
// presentation defaults (display name / unit / anomaly). Project-specific refs
// (data sources, fact tables, events, measure columns) are deliberately left
// empty — the user still points the metric at their own data.
export interface MetricTemplateSeed {
  /** Which metric kind the template starts on. */
  kind: MetricKind
  /** Prefilled display name (also derives the internal name until edited). */
  displayName: string
  /** Prefilled display unit; '' leaves it blank. */
  unit: string
  /** Anomaly-detection default. */
  anomalyDetection: boolean
  /** Collection interval — only meaningful for the sql / fact kinds. */
  interval?: MetricScanInterval
  /** Event-composition shape (single | ratio | per_distinct_user). */
  composition?: MetricComposition
  /** Fact composition (single | ratio). */
  factComposition?: 'single' | 'ratio'
  /** Fact single-operand aggregation. */
  aggregation?: MetricAggregation
  /**
   * Which starter SQL scaffold the `sql` kind begins from. The SQL TEXT is not
   * stored on the seed, because there is no single text that works: it is
   * rendered per warehouse by {@link starterSql}.
   */
  sqlTemplate?: SqlTemplateId
  /** Prefilled SQL time/bucket column for the sql kind. */
  sqlTimeColumn?: string
}

/** The starter SQL scaffolds, each renderable for any supported warehouse. */
export type SqlTemplateId = 'daily-active-users' | 'event-volume'

/** Every warehouse a starter template can be rendered for. */
export const DB_TYPES: readonly DbType[] = [
  'clickhouse',
  'postgres',
  'bigquery',
  'databricks',
  'snowflake',
  'greenplum',
  'redshift',
  'trino',
  'athena',
  'synthetic',
]

/**
 * The per-dialect time-bucket expression, mirroring each backend adapter's
 * `_bucket_expression` so a starter query buckets exactly the way a collection
 * will. All three agree with `tripl.core.bucketing.floor_to_bucket`: UTC, and
 * anchored at the Unix epoch for these sub-week intervals.
 *
 * This is the whole point of the module. `date_trunc('day', ts)` — the single
 * form every template used to emit — is valid on ClickHouse and PostgreSQL and a
 * hard error on BigQuery, whose GoogleSQL `DATE_TRUNC` takes `(date_expr,
 * date_part)` and answers the string-first form with "A valid date part name is
 * required but found created_at". Every expression below was executed against a
 * real engine (ClickHouse 26.8, PostgreSQL 18, and BigQuery's own ZetaSQL
 * analyzer via the emulator).
 */
function bucketExpression(db: DbType | undefined, unit: 'day' | 'hour'): string {
  switch (db) {
    case 'postgres':
      return `date_bin(INTERVAL '1 ${unit}', created_at, TIMESTAMPTZ '1970-01-01 00:00:00+00:00')`
    case 'bigquery':
      return `TIMESTAMP_TRUNC(created_at, ${unit.toUpperCase()}, 'UTC')`
    // Databricks: Spark's string-first date_trunc. The connection pins the
    // session time zone to UTC, so this truncates in UTC like the others.
    case 'databricks':
      return `date_trunc('${unit.toUpperCase()}', created_at)`
    // Snowflake: the same string-first DATE_TRUNC; the connection pins the
    // session TIMEZONE to UTC.
    case 'snowflake':
      return `DATE_TRUNC('${unit.toUpperCase()}', created_at)`
    // Greenplum (PostgreSQL 9.4 / 12) and Redshift predate date_bin; the
    // connection pins the session time zone to UTC, so date_trunc truncates in UTC.
    case 'greenplum':
    case 'redshift':
      return `date_trunc('${unit}', created_at)`
    // Trino and Athena (engine version 3 is Trino SQL): date_trunc truncates a
    // zoned timestamp in its OWN zone, so the value is moved to UTC first. A
    // zone-less column is read in the session zone, which is UTC on both.
    case 'trino':
    case 'athena':
      return `date_trunc('${unit}', created_at AT TIME ZONE 'UTC')`
    // ClickHouse, the synthetic demo warehouse (which mimics ClickHouse
    // semantics), and "no source picked yet" all use the ClickHouse form. The
    // moment a source is selected the SQL is re-rendered for it, so an
    // unselected source only ever shows a placeholder the user has not edited.
    default:
      return `toStartOfInterval(created_at, INTERVAL 1 ${unit.toUpperCase()}, 'UTC')`
  }
}

/**
 * Render a starter scaffold as SQL valid for the selected warehouse.
 *
 * `FROM events` is a placeholder the user repoints at their own table; what is
 * dialect-specific — and what used to be wrong — is the bucket expression.
 * Deliberately free of SQL comments: the backend's read-only gate
 * (`validate_select_sql_safety`) rejects every comment marker outright.
 */
export function starterSql(id: SqlTemplateId, db: DbType | undefined): string {
  if (id === 'daily-active-users') {
    return [
      `SELECT ${bucketExpression(db, 'day')} AS bucket,`,
      '       count(DISTINCT user_id) AS value',
      'FROM events',
      'GROUP BY 1',
      'ORDER BY 1',
    ].join('\n')
  }
  return [
    `SELECT ${bucketExpression(db, 'hour')} AS bucket,`,
    '       count(*) AS value',
    'FROM events',
    'GROUP BY 1',
    'ORDER BY 1',
  ].join('\n')
}

/**
 * Whether `sql` is still untouched starter output for `id` — i.e. it matches what
 * {@link starterSql} renders for SOME warehouse.
 *
 * This is the guard that makes re-rendering on a data-source switch safe. Once
 * the user has typed a single character into the editor the SQL stops matching
 * any dialect's rendering, and their work is never silently overwritten. It stays
 * true across a switch we performed ourselves (the text is then the new dialect's
 * rendering), so a user who flips PG -> BigQuery -> PG keeps getting valid SQL.
 */
export function isPristineStarterSql(id: SqlTemplateId, sql: string): boolean {
  return DB_TYPES.some(db => starterSql(id, db) === sql)
}

export interface MetricTemplate {
  /** Stable id (React key / template lookup). */
  id: string
  /** Card title, also the button's accessible name. */
  label: string
  /** One-line description shown under the title. */
  description: string
  /** lucide icon component rendered in the card. */
  icon: LucideIcon
  /** The editable-field seed applied to the form when picked. */
  seed: MetricTemplateSeed
}

// Starter templates surfaced on the New metric screen. Ordered easiest-first so
// an empty project has a fast path to its first metric.
export const METRIC_TEMPLATES: readonly MetricTemplate[] = [
  {
    id: 'daily-active-users',
    label: 'Daily active users',
    description: 'Distinct users per day from a warehouse query.',
    icon: Users,
    seed: {
      kind: 'sql',
      displayName: 'Daily active users',
      unit: '',
      anomalyDetection: true,
      interval: '1d',
      sqlTemplate: 'daily-active-users',
      sqlTimeColumn: 'bucket',
    },
  },
  {
    id: 'conversion',
    label: 'Conversion A→B',
    description: 'Ratio of one event to another — a funnel step.',
    icon: ArrowRightLeft,
    seed: {
      kind: 'event_composition',
      displayName: 'Conversion',
      unit: '%',
      anomalyDetection: true,
      composition: 'ratio',
    },
  },
  {
    id: 'revenue',
    label: 'Revenue',
    description: 'Sum a measure column from a fact table.',
    icon: DollarSign,
    seed: {
      kind: 'fact',
      displayName: 'Revenue',
      unit: '$',
      anomalyDetection: true,
      interval: '1d',
      factComposition: 'single',
      aggregation: 'sum',
    },
  },
  {
    id: 'average-order-value',
    label: 'Average order value',
    description: 'Average a measure column from a fact table.',
    icon: ShoppingCart,
    seed: {
      kind: 'fact',
      displayName: 'Average order value',
      unit: '$',
      anomalyDetection: true,
      interval: '1d',
      factComposition: 'single',
      aggregation: 'avg',
    },
  },
  {
    id: 'error-rate',
    label: 'Error rate',
    description: 'Share of events that failed — a ratio of two event series.',
    icon: AlertTriangle,
    seed: {
      kind: 'event_composition',
      displayName: 'Error rate',
      unit: '%',
      anomalyDetection: true,
      composition: 'ratio',
    },
  },
  {
    id: 'event-volume',
    label: 'Event volume',
    description: 'Count rows per bucket with a warehouse query.',
    icon: Activity,
    seed: {
      kind: 'sql',
      displayName: 'Event volume',
      unit: '',
      anomalyDetection: true,
      interval: '1h',
      sqlTemplate: 'event-volume',
      sqlTimeColumn: 'bucket',
    },
  },
]
