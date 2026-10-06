import { describe, expect, it } from 'vitest'
import type { DbType } from '@/types/dataSources'
import {
  DB_TYPES,
  METRIC_TEMPLATES,
  isPristineStarterSql,
  starterSql,
  type SqlTemplateId,
} from './metricTemplates'

const SQL_TEMPLATE_IDS: SqlTemplateId[] = ['daily-active-users', 'event-volume']

describe('starterSql', () => {
  it('emits a bucket expression each warehouse actually has', () => {
    // These are the exact forms executed against ClickHouse 26.8, PostgreSQL 18 and
    // BigQuery's ZetaSQL analyzer. The old single `date_trunc('day', ts)` form ran on
    // the first two and was a hard error on BigQuery ("A valid date part name is
    // required but found created_at"), which is the bug this module exists to fix.
    expect(starterSql('daily-active-users', 'clickhouse')).toContain(
      "toStartOfInterval(created_at, INTERVAL 1 DAY, 'UTC')",
    )
    expect(starterSql('daily-active-users', 'postgres')).toContain(
      "date_bin(INTERVAL '1 day', created_at, TIMESTAMPTZ '1970-01-01 00:00:00+00:00')",
    )
    expect(starterSql('daily-active-users', 'bigquery')).toContain(
      "TIMESTAMP_TRUNC(created_at, DAY, 'UTC')",
    )
    // Spark's string-first form; the connection pins the session zone to UTC.
    expect(starterSql('daily-active-users', 'databricks')).toContain(
      "date_trunc('DAY', created_at)",
    )
  })

  it('buckets the event-volume template hourly, per dialect', () => {
    expect(starterSql('event-volume', 'clickhouse')).toContain('INTERVAL 1 HOUR')
    expect(starterSql('event-volume', 'postgres')).toContain("INTERVAL '1 hour'")
    expect(starterSql('event-volume', 'bigquery')).toContain(
      "TIMESTAMP_TRUNC(created_at, HOUR, 'UTC')",
    )
    expect(starterSql('event-volume', 'databricks')).toContain("date_trunc('HOUR', created_at)")
  })

  it('never emits date_trunc() for BigQuery', () => {
    for (const id of SQL_TEMPLATE_IDS) {
      expect(starterSql(id, 'bigquery')).not.toMatch(/date_trunc/i)
    }
  })

  it('treats the synthetic demo warehouse as ClickHouse', () => {
    for (const id of SQL_TEMPLATE_IDS) {
      expect(starterSql(id, 'synthetic')).toBe(starterSql(id, 'clickhouse'))
    }
  })

  it('falls back to the ClickHouse form when no source is selected yet', () => {
    for (const id of SQL_TEMPLATE_IDS) {
      expect(starterSql(id, undefined)).toBe(starterSql(id, 'clickhouse'))
    }
  })

  it('projects the bucket and value columns the backend requires', () => {
    for (const id of SQL_TEMPLATE_IDS) {
      for (const db of DB_TYPES) {
        const sql = starterSql(id, db)
        expect(sql).toContain('AS bucket')
        expect(sql).toContain('AS value')
      }
    }
  })

  it('contains no SQL comment markers, which the read-only gate rejects outright', () => {
    for (const id of SQL_TEMPLATE_IDS) {
      for (const db of DB_TYPES) {
        const sql = starterSql(id, db)
        expect(sql).not.toContain('--')
        expect(sql).not.toContain('/*')
        expect(sql).not.toContain('#')
        expect(sql).not.toContain(';')
      }
    }
  })

  it('renders a genuinely different query per engine family', () => {
    const rendered = new Set(
      (['clickhouse', 'postgres', 'bigquery', 'databricks'] as DbType[]).map(db =>
        starterSql('daily-active-users', db),
      ),
    )
    expect(rendered.size).toBe(4)
  })
})

describe('isPristineStarterSql', () => {
  it('recognises its own output for every warehouse', () => {
    for (const id of SQL_TEMPLATE_IDS) {
      for (const db of DB_TYPES) {
        expect(isPristineStarterSql(id, starterSql(id, db))).toBe(true)
      }
    }
  })

  it('is false once the user has edited the SQL', () => {
    const edited = `${starterSql('daily-active-users', 'postgres')}\nLIMIT 10`
    expect(isPristineStarterSql('daily-active-users', edited)).toBe(false)
  })

  it('is false for a one-character change', () => {
    const base = starterSql('event-volume', 'clickhouse')
    expect(isPristineStarterSql('event-volume', base.replace('events', 'event'))).toBe(false)
  })

  it('does not confuse one template for another', () => {
    expect(isPristineStarterSql('event-volume', starterSql('daily-active-users', 'bigquery'))).toBe(
      false,
    )
  })

  it('is false for empty / hand-written SQL', () => {
    expect(isPristineStarterSql('daily-active-users', '')).toBe(false)
    expect(isPristineStarterSql('daily-active-users', 'SELECT 1')).toBe(false)
  })
})

describe('METRIC_TEMPLATES', () => {
  it('gives every sql-kind template a dialect-rendered scaffold, not literal SQL', () => {
    const sqlTemplates = METRIC_TEMPLATES.filter(t => t.seed.kind === 'sql')
    expect(sqlTemplates.length).toBeGreaterThan(0)
    for (const template of sqlTemplates) {
      expect(template.seed.sqlTemplate).toBeDefined()
      expect(template.seed.sqlTimeColumn).toBe('bucket')
    }
  })

  it('has no sql-kind template carrying a hardcoded dialect-specific query', () => {
    for (const template of METRIC_TEMPLATES) {
      expect(template.seed).not.toHaveProperty('metricSql')
    }
  })
})
