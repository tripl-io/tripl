import { useMemo, useState } from 'react'
import { useQueries, useQuery } from '@tanstack/react-query'
import { BookOpen } from 'lucide-react'
import { Link } from 'react-router-dom'

import { dataSourcesApi } from '@/api/dataSources'
import { eventsApi } from '@/api/events'
import { eventTypesApi } from '@/api/eventTypes'
import { factTablesApi } from '@/api/factTables'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import { Chip } from '@/components/primitives/chip'
import { CodeToken } from '@/components/primitives/code-token'
import { LazySqlEditor } from '@/components/sql-editor-lazy'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { formatDateTime } from '@/lib/datetime'
import { factColumnValueKind } from '@/lib/factColumnValueKind'
import {
  VALUELESS_CONDITION_OPERATORS,
  conditionOperatorLabel,
  readFactOperandConfig,
  type FactConditionConfig,
  type FactOperandConfig,
} from '@/lib/factOperandConfig'
import { eventNameLabel } from '@/lib/eventName'
import { METRIC_INTERVAL_LABEL } from '@/lib/metricFormat'
import { useCanWriteProject } from '@/lib/permissions'
import { METRIC_KIND_LABEL } from '@/types'
import type { MetricDefinitionDetailResponse } from '@/types'
import {
  dataSourcesKey,
  eventKey,
  eventTypesKey,
  factTableKey,
  factTablesKey,
  metricGeneratedSqlForMetricKey,
} from '@/lib/queryKeys'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

/** Names are best-effort; when a lookup misses we fall back to a short id. */
const SHORT_ID_LENGTH = 8
/** Lookup lists change rarely; a minute of staleness avoids refetch churn. */
const LOOKUP_STALE_TIME_MS = 60_000

function shortId(id: string): string {
  return id.slice(0, SHORT_ID_LENGTH)
}

function configString(
  config: Record<string, unknown>,
  key: string,
): string | null {
  const value = config[key]
  return typeof value === 'string' && value ? value : null
}

/** One side of a fact expression: `<aggregation>(<column|*>) from <table>`. */
interface FactOperandView {
  factTableId: string | null
  aggregation: string
  column: string | null
  filters: FactFiltersView
}

/**
 * The three row-filter inputs of one fact operand, ANDed at collection time:
 * named row filters (labels of the fact table's stored filters), visual
 * conditions, and a free-text SQL WHERE fragment.
 */
type FactFiltersView = Pick<FactOperandConfig, 'rowFilters' | 'conditions' | 'filterSql'>

function hasFilters(filters: FactFiltersView): boolean {
  return (
    filters.rowFilters.length > 0
    || filters.conditions.length > 0
    || filters.filterSql !== null
  )
}

function conditionScalarText(value: unknown, columnType?: string | null): string {
  if (typeof value !== 'string') return String(value)
  const kind = factColumnValueKind(columnType)
  if (kind === 'number' && value.trim() !== '' && Number.isFinite(Number(value))) {
    return value
  }
  if (kind === 'boolean' && /^(?:true|false)$/i.test(value)) {
    return value.toLowerCase()
  }
  return `'${value}'`
}

/** SQL-ish one-liner for a condition, e.g. `platform = 'ios'` / `plan in ('a', 'b')`. */
function conditionText(condition: FactConditionConfig, columnType?: string | null): string {
  const operator = conditionOperatorLabel(condition.operator)
  if (
    VALUELESS_CONDITION_OPERATORS.has(condition.operator)
    || condition.value === null
    || condition.value === undefined
  ) {
    return `${condition.column} ${operator}`
  }
  if (Array.isArray(condition.value)) {
    const values = condition.value
      .map(value => conditionScalarText(value, columnType))
      .join(', ')
    return `${condition.column} ${operator} (${values})`
  }
  return `${condition.column} ${operator} ${conditionScalarText(condition.value, columnType)}`
}

/** Display shape of one parsed operand (shared parser: lib/factOperandConfig). */
function toOperandView(config: FactOperandConfig): FactOperandView {
  return {
    factTableId: config.factTableId,
    aggregation: config.aggregation,
    column: config.aggregation === 'count_distinct' ? config.distinctColumn : config.measureColumn,
    filters: config,
  }
}

/** A ratio's `config.numerator` / `config.denominator` block, when present. */
function readFactOperand(value: unknown): FactOperandView | null {
  if (!value || typeof value !== 'object') return null
  return toOperandView(readFactOperandConfig(value))
}

function referencedFactTableIds(definition: MetricDefinitionDetailResponse): string[] {
  if (definition.kind !== 'fact') return []
  if (definition.composition !== 'ratio') {
    return definition.fact_table_id ? [definition.fact_table_id] : []
  }
  const ids = [
    readFactOperand(definition.config['numerator'])?.factTableId,
    readFactOperand(definition.config['denominator'])?.factTableId,
  ].filter((id): id is string => !!id)
  return [...new Set(ids)]
}

interface MetricDefinitionCardProps {
  slug: string
  definition: MetricDefinitionDetailResponse
}

/**
 * Compact "Definition" summary for the catalog-metric drilldown: what the
 * metric computes (kind + human-readable expression + row filters) plus its
 * collection settings, without opening the edit form. Name lookups (fact tables, events,
 * data sources) are best-effort — failures degrade to short ids, so these
 * queries stay out of the page-level error state.
 */
export function MetricDefinitionCard({ slug, definition }: MetricDefinitionCardProps) {
  const { kind, config } = definition

  const factTablesQuery = useQuery({
    queryKey: factTablesKey(slug),
    queryFn: () => factTablesApi.list(slug),
    enabled: kind === 'fact',
    staleTime: LOOKUP_STALE_TIME_MS,
  })
  // Names resolved BY ID, not looked up in the first page of the events list:
  // that page is the endpoint's default 200, so a metric on a later event
  // painted an 8-char id instead of its name.
  const eventIds = [definition.numerator_event_id, definition.denominator_event_id].filter(
    (id): id is string => kind === 'event_composition' && !!id,
  )
  const eventNameById = useQueries({
    queries: eventIds.map(id => ({
      queryKey: eventKey(slug, null, id),
      queryFn: () => eventsApi.get(slug, id),
      staleTime: LOOKUP_STALE_TIME_MS,
    })),
    combine: results =>
      new Map(
        results.flatMap(result =>
          result.data ? [[result.data.id, eventNameLabel(result.data.name)] as const] : [],
        ),
      ),
  })
  // A side may reference a whole event type instead of one event.
  const referencesEventType =
    kind === 'event_composition'
    && !!(definition.numerator_event_type_id || definition.denominator_event_type_id)
  const eventTypesQuery = useQuery({
    queryKey: eventTypesKey(slug, null),
    queryFn: () => eventTypesApi.list(slug),
    enabled: referencesEventType,
    staleTime: LOOKUP_STALE_TIME_MS,
  })
  const dataSourcesQuery = useQuery({
    queryKey: dataSourcesKey(),
    queryFn: () => dataSourcesApi.list(),
    enabled: !!definition.data_source_id,
    staleTime: LOOKUP_STALE_TIME_MS,
  })
  const factTableIds = useMemo(() => referencedFactTableIds(definition), [definition])
  const factTableDetailQueries = useQueries({
    queries: factTableIds.map(id => ({
      queryKey: factTableKey(slug, id),
      queryFn: () => factTablesApi.get(slug, id),
      staleTime: LOOKUP_STALE_TIME_MS,
    })),
  })

  const factTableNameById = useMemo(
    () => {
      const names = new Map(
        (factTablesQuery.data?.items ?? []).map(table => [table.id, table.display_name]),
      )
      for (const query of factTableDetailQueries) {
        if (query.data) names.set(query.data.id, query.data.display_name)
      }
      return names
    },
    [factTableDetailQueries, factTablesQuery.data],
  )
  const factTableColumnType = (factTableId: string | null, column: string): string | null => {
    if (!factTableId) return null
    const table = factTableDetailQueries.find(query => query.data?.id === factTableId)?.data
    return table?.columns.find(candidate => candidate.name === column)?.type ?? null
  }
  const factTableName = (id: string | null): string =>
    id ? factTableNameById.get(id) ?? shortId(id) : '—'
  const eventTypeNameById = new Map(
    (eventTypesQuery.data ?? []).map(type => [type.id, type.display_name]),
  )
  // One side of an event composition: its event, else its event type.
  const eventRefName = (eventId: string | null, eventTypeId: string | null): string => {
    if (eventId) return eventNameById.get(eventId) ?? shortId(eventId)
    if (eventTypeId) return `type · ${eventTypeNameById.get(eventTypeId) ?? shortId(eventTypeId)}`
    return '—'
  }
  const dataSourceName = definition.data_source_id
    ? dataSourcesQuery.data?.find(source => source.id === definition.data_source_id)?.name
      ?? shortId(definition.data_source_id)
    : null

  return (
    // The shared section-card geometry: a header bar with the
    // 12.5px h2, then the body.
    <Card>
      <CardHeader className="flex-row flex-wrap items-center gap-2">
        <BookOpen aria-hidden="true" className="size-4 text-fg-tertiary" />
        <CardTitle as="h2">Definition</CardTitle>
        {/* The metric's kind is a category tag: an outline chip. */}
        <Chip variant="outline" size="xs">{METRIC_KIND_LABEL[kind]}</Chip>
      </CardHeader>
      <CardContent className="space-y-3">

        {kind === 'sql' && <SqlExpression config={config} />}
        {kind === 'fact' && (
          <FactExpression
            slug={slug}
            definition={definition}
            factTableName={factTableName}
            factTableColumnType={factTableColumnType}
          />
        )}
        {kind === 'event_composition' && (
          <EventCompositionExpression definition={definition} eventRefName={eventRefName} />
        )}

        <div className="flex flex-wrap items-center gap-1.5">
          {definition.interval && (
            <Chip size="xs" variant="outline">
              {METRIC_INTERVAL_LABEL[definition.interval]}
            </Chip>
          )}
          <MetricSchedule slug={slug} definition={definition} />
          {dataSourceName && (
            <Chip size="xs" variant="outline">source · {dataSourceName}</Chip>
          )}
          {definition.unit && (
            <Chip size="xs" variant="outline">unit · {definition.unit}</Chip>
          )}
          {definition.breakdown_columns.map(column => (
            <Chip key={column} size="xs" variant="outline" className="font-mono">
              by {column}
            </Chip>
          ))}
        </div>
      </CardContent>
    </Card>
  )
}

function SqlExpression({ config }: { config: Record<string, unknown> }) {
  const metricSql = configString(config, 'metric_sql')
  const timeColumn = configString(config, 'time_column')
  const valueColumn = configString(config, 'value_column')
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-1.5 text-body-sm text-fg-tertiary">
        {/* Column names are identifiers: code tokens, not pills. A
            column the metric does not use is left out, not shown as an
            empty "–" token. */}
        {timeColumn && (
          <>
            <span>time</span>
            <CodeToken>{timeColumn}</CodeToken>
          </>
        )}
        {valueColumn && (
          <>
            <span>value</span>
            <CodeToken>{valueColumn}</CodeToken>
          </>
        )}
      </div>
      {metricSql && (
        <details className="rounded-md border">
          <summary className="cursor-pointer select-none px-3 py-1.5 text-body-sm font-medium text-fg-tertiary">
            Show SQL
          </summary>
          <div className="border-t p-2">
            <LazySqlEditor
              value={metricSql}
              onChange={() => undefined}
              ariaLabel="Metric SQL"
              minHeight="120px"
              readOnly
            />
          </div>
        </details>
      )}
    </div>
  )
}

/**
 * Row-filter summary for one fact operand: each named filter, visual condition,
 * and the free-text SQL fragment gets its own labelled line, indented under the
 * operand's expression. Renders nothing when the operand is unfiltered.
 */
function FactFilterLines({
  filters,
  columnType,
}: {
  filters: FactFiltersView
  columnType: (column: string) => string | null
}) {
  if (!hasFilters(filters)) return null
  return (
    <ul className="space-y-0.5 pl-4 text-body-sm">
      {filters.rowFilters.map(name => (
        <li key={`row-filter-${name}`}>
          <span className="text-fg-tertiary">filter · </span>
          {name}
        </li>
      ))}
      {filters.conditions.map((condition, index) => (
        <li key={`condition-${index}`}>
          <span className="text-fg-tertiary">{index === 0 ? 'where' : 'and'} </span>
          <code className="font-mono">
            {conditionText(condition, columnType(condition.column))}
          </code>
        </li>
      ))}
      {filters.filterSql && (
        <li>
          <span className="text-fg-tertiary">
            {filters.conditions.length === 0 ? 'where' : 'and'}{' '}
          </span>
          <code className="font-mono">{filters.filterSql}</code>
        </li>
      )}
    </ul>
  )
}

function FactOperandBlock({
  operand,
  slug,
  factTableName,
  factTableColumnType,
}: {
  operand: FactOperandView
  slug: string
  factTableName: (id: string | null) => string
  factTableColumnType: (factTableId: string | null, column: string) => string | null
}) {
  return (
    <div className="space-y-0.5">
      <p className="font-mono text-body">
        {operand.aggregation}({operand.column ?? '*'})
        <span className="text-fg-tertiary"> from </span>
        {operand.factTableId ? (
          <Link
            to={projectPath(currentOrgSlug(), slug, `/metrics/fact-tables/${operand.factTableId}/edit`)}
            className="underline decoration-fg-tertiary/50 underline-offset-2 hover:decoration-current"
          >
            {factTableName(operand.factTableId)}
          </Link>
        ) : '—'}
      </p>
      <FactFilterLines
        filters={operand.filters}
        columnType={column => factTableColumnType(operand.factTableId, column)}
      />
    </div>
  )
}

function FactExpression({
  slug,
  definition,
  factTableName,
  factTableColumnType,
}: {
  slug: string
  definition: MetricDefinitionDetailResponse
  factTableName: (id: string | null) => string
  factTableColumnType: (factTableId: string | null, column: string) => string | null
}) {
  const { config } = definition
  if (definition.composition === 'ratio') {
    const numerator = readFactOperand(config['numerator'])
    const denominator = readFactOperand(config['denominator'])
    return (
      <div className="space-y-2">
        {numerator && (
          <FactOperandBlock
            slug={slug}
            operand={numerator}
            factTableName={factTableName}
            factTableColumnType={factTableColumnType}
          />
        )}
        <p aria-hidden="true" className="text-body text-fg-tertiary">÷</p>
        {denominator && (
          <FactOperandBlock
            slug={slug}
            operand={denominator}
            factTableName={factTableName}
            factTableColumnType={factTableColumnType}
          />
        )}
        <GeneratedBatchSqlDisclosure slug={slug} metricId={definition.id} />
      </div>
    )
  }
  const single = toOperandView(
    readFactOperandConfig(config, {
      factTableId: definition.fact_table_id,
      aggregation: definition.aggregation,
    }),
  )
  return (
    <div className="space-y-2">
      <FactOperandBlock
        slug={slug}
        operand={single}
        factTableName={factTableName}
        factTableColumnType={factTableColumnType}
      />
      <GeneratedBatchSqlDisclosure slug={slug} metricId={definition.id} />
    </div>
  )
}

function GeneratedBatchSqlDisclosure({ slug, metricId }: { slug: string; metricId: string }) {
  // Shown to viewers too: the generated-SQL endpoint has the metric read's gate,
  // because the SQL is compiled from config that read already returns.
  const [open, setOpen] = useState(false)
  const query = useQuery({
    queryKey: metricGeneratedSqlForMetricKey(slug, metricId),
    queryFn: () => metricsCatalogApi.getGeneratedSql(slug, metricId),
    enabled: open,
    staleTime: LOOKUP_STALE_TIME_MS,
  })
  const queries = query.data?.queries ?? []
  return (
    <details
      className="rounded-md border"
      onToggle={event => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer select-none px-3 py-1.5 text-body-sm font-medium text-fg-tertiary">
        Generated batch SQL
      </summary>
      <div className="space-y-2 border-t p-2">
        <p className="px-1 text-body-sm text-fg-tertiary">
          Primary queries executed by Collect now. Compatible aggregates from dependent metrics
          are folded into one query per fact table, interval, and replay chunk.
        </p>
        {query.isFetching && <p className="px-1 text-body-sm text-fg-tertiary">Loading SQL…</p>}
        {query.isError && (
          <p role="alert" className="px-1 text-body-sm text-destructive">
            Could not generate batch SQL.
          </p>
        )}
        {query.isSuccess && queries.length === 0 && (
          <p className="px-1 text-body-sm text-fg-tertiary">No generated SQL is available.</p>
        )}
        {queries.map((item, index) => {
          const editorLabel = queries.length > 1
            ? `${item.label} generated batch SQL`
            : 'Generated batch SQL'
          return (
            <div
              key={`${item.fact_table_id}-${item.interval}-${item.window_from}-${index}`}
              className="space-y-1"
            >
              <p className="px-1 text-caption text-fg-tertiary">
                <span className="font-medium text-foreground">{item.label}</span>
                {' · '}{item.metric_ids.length} metric{item.metric_ids.length === 1 ? '' : 's'}
                {' · '}{formatDateTime(item.window_from)} → {formatDateTime(item.window_to)}
              </p>
              <LazySqlEditor
                value={item.sql}
                onChange={() => undefined}
                ariaLabel={editorLabel}
                minHeight="120px"
                readOnly
              />
            </div>
          )
        })}
        {query.data?.breakdown_queries_omitted && (
          <p className="px-1 text-caption text-fg-tertiary">
            Breakdown queries are generated separately and are not shown here.
          </p>
        )}
      </div>
    </details>
  )
}

function MetricSchedule({ slug, definition }: { slug: string; definition: MetricDefinitionDetailResponse }) {
  const canEdit = useCanWriteProject()
  // A draft is not collected whatever its interval; the header's Activate is
  // the way out, so this only says why nothing runs (#246).
  if (definition.status === 'draft') {
    return <span className="text-body-sm text-fg-tertiary">Not collected while draft</span>
  }
  // An active metric with no interval is a dead end unless the line leads to
  // where the schedule is set — for someone who can set it.
  if (definition.status === 'active' && !definition.interval && canEdit) {
    return (
      <Link
        to={projectPath(currentOrgSlug(), slug, `/metrics/${definition.id}/edit`)}
        className="text-body-sm underline decoration-fg-tertiary/50 underline-offset-2 hover:decoration-current"
      >
        Set a schedule
      </Link>
    )
  }
  if (definition.status !== 'active') {
    return <span className="text-body-sm text-fg-tertiary">Not scheduled</span>
  }
  if (definition.collection_due) {
    return <span className="text-body-sm font-medium text-warning">Due now</span>
  }
  if (definition.next_collection_at) {
    return (
      <span className="text-body-sm text-fg-tertiary">
        Next update {formatDateTime(definition.next_collection_at)}
      </span>
    )
  }
  return <span className="text-body-sm text-fg-tertiary">Not scheduled</span>
}

function EventCompositionExpression({
  definition,
  eventRefName,
}: {
  definition: MetricDefinitionDetailResponse
  eventRefName: (eventId: string | null, eventTypeId: string | null) => string
}) {
  const numerator = eventRefName(definition.numerator_event_id, definition.numerator_event_type_id)
  const userIdColumn = configString(definition.config, 'user_id_column')
  if (definition.composition === 'ratio') {
    return (
      <p className="text-body">
        <span className="font-mono">{numerator}</span>
        <span className="text-fg-tertiary"> ÷ </span>
        <span className="font-mono">
          {eventRefName(definition.denominator_event_id, definition.denominator_event_type_id)}
        </span>
      </p>
    )
  }
  if (definition.composition === 'per_distinct_user') {
    return (
      <div className="flex flex-wrap items-center gap-1.5">
        <p className="text-body">
          <span className="text-fg-tertiary">distinct users of </span>
          <span className="font-mono">{numerator}</span>
        </p>
        {userIdColumn && (
          <Chip size="xs" variant="outline" className="font-mono">
            user id · {userIdColumn}
          </Chip>
        )}
      </div>
    )
  }
  return <p className="font-mono text-body">{numerator}</p>
}
