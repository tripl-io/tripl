import { CodeToken } from '@/components/primitives/code-token'
import { SCard } from '@/components/settings/kit'
import { ReadOnlyDefinition, type DefinitionItem } from '@/components/states'
import { countOf } from '@/lib/plural'
import type { DataSource, EventType, ScanConfig } from '@/types'
import { INTERVAL_LABEL } from './scanLayoutConstants'
import { SCAN_MODE_DETAIL_LABEL, scanModeOf } from './scanMode'

function tokens(values: readonly string[]) {
  if (values.length === 0) return null
  return (
    <span className="flex flex-wrap gap-1">
      {values.map(value => (
        <CodeToken key={value}>{value}</CodeToken>
      ))}
    </span>
  )
}

function token(value: string | null) {
  return value ? <CodeToken>{value}</CodeToken> : null
}

/** A cap left empty falls back to the instance default; say so, not "—". */
function limit(value: number | null) {
  return value == null ? 'Instance default' : value.toLocaleString()
}

/**
 * A scan's configuration for someone who cannot change it. The tab
 * used to render the whole edit form inside a disabled fieldset — live
 * borders, pickers, preview buttons and author hints — for a reader who can
 * only read. This is the same definition as a description list; the caller
 * puts the `ReadOnlyNotice` above it.
 */
export function ScanConfigReadView({
  scanConfig: sc,
  dataSources,
  eventTypes,
}: {
  scanConfig: ScanConfig
  dataSources: readonly DataSource[]
  eventTypes: readonly EventType[]
}) {
  const source = dataSources.find(ds => ds.id === sc.data_source_id)
  const eventType = eventTypes.find(et => et.id === sc.event_type_id)
  const mode = scanModeOf(sc)
  const monitoring = mode === 'monitoring'
  // The preset's naming is its two columns; the fields it derives from them
  // (format, kept JSON values, group rules) would only repeat them.
  const preset = sc.setup_preset === 'event_properties'
  const naming: DefinitionItem[] = preset
    ? [
        { label: 'Setup', value: 'Event + properties' },
        { label: 'Event column', value: token(sc.event_name_column ?? null) },
        { label: 'Properties column', value: token(sc.properties_column ?? null) },
        { label: 'Event type', value: eventType?.display_name ?? null },
      ]
    : [
        sc.event_type_id
          ? { label: 'Event type', value: eventType?.display_name ?? null }
          : { label: 'Event type column', value: token(sc.event_type_column) },
        { label: 'Event name format', value: token(sc.event_name_format) },
      ]

  const items: DefinitionItem[] = [
    { label: 'What it does', value: SCAN_MODE_DETAIL_LABEL[mode] },
    { label: 'Data source', value: source?.name ?? null },
    {
      label: 'Query',
      block: true,
      value: (
        <pre
          aria-label="Scan query"
          className="m-0 max-h-[360px] overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-bg-sunken px-3 py-2 font-mono text-caption text-fg"
        >
          {sc.base_query}
        </pre>
      ),
    },
    ...(sc.json_string_columns?.length
      ? [{ label: 'Parsed as JSON', value: tokens(sc.json_string_columns) }]
      : []),
    ...naming,
    { label: 'Time column', value: token(sc.time_column) },
    {
      label: 'Schedule',
      value: sc.interval ? (INTERVAL_LABEL[sc.interval] ?? sc.interval) : 'Runs only when started',
    },
    {
      label: 'Lookback',
      value: sc.scan_lookback_hours ? `Last ${countOf(sc.scan_lookback_hours, 'hour', 'hours')}` : 'Whole query',
    },
    ...(preset ? [] : [{ label: 'JSON values kept', value: tokens(sc.json_value_paths) }]),
    {
      label: 'Event groups',
      block: sc.event_group_rules.length > 0,
      value:
        sc.event_group_rules.length > 0 ? (
          <ul className="m-0 list-none space-y-1 p-0">
            {sc.event_group_rules.map(rule => (
              <li key={rule.name}>
                <span className="font-medium">{rule.name}</span>{' '}
                <span className="text-fg-tertiary">
                  ({countOf(rule.conditions.length, 'condition', 'conditions')},{' '}
                  {rule.condition_logic === 'all' ? 'all must match' : 'any may match'})
                </span>
              </li>
            ))}
          </ul>
        ) : null,
    },
    ...(monitoring
      ? [
          { label: 'Metric breakdowns', value: tokens(sc.metric_breakdown_columns) },
          {
            label: 'Values per breakdown',
            value: sc.metric_breakdown_values_limit == null ? 'No limit' : sc.metric_breakdown_values_limit.toLocaleString(),
          },
          { label: 'Distribution drift', value: tokens(sc.distribution_drift_fields) },
          {
            label: 'Replay chunk',
            value: sc.replay_chunk_interval ? (INTERVAL_LABEL[sc.replay_chunk_interval] ?? sc.replay_chunk_interval) : null,
          },
        ]
      : []),
    { label: 'App version column', value: token(sc.app_version_column) },
    { label: 'Platform column', value: token(sc.platform_column) },
    { label: 'Cardinality threshold', value: sc.cardinality_threshold.toLocaleString() },
    { label: 'Scan row cap', value: limit(sc.scan_row_limit) },
    ...(monitoring ? [{ label: 'Metrics row cap', value: limit(sc.metrics_row_limit) }] : []),
  ]

  return (
    <SCard title="Scan settings">
      <div className="px-4 py-3.5">
        <ReadOnlyDefinition items={items} />
      </div>
    </SCard>
  )
}
