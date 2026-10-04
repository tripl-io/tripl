import { useState } from 'react'
import { ChevronRight } from 'lucide-react'
import { ColumnCheckboxPicker } from '@/components/column-checkbox-picker'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { ColumnSuggestInput } from '@/components/column-suggest'
import { SCard, ToggleRow, Field } from '@/components/settings/kit'
import type { MetricDraft } from './metricDraft'
import { examplePlaceholder } from '@/components/forms/placeholders'

interface MonitoringFieldsProps {
  draft: MetricDraft
  patch: (next: Partial<MetricDraft>) => void
  /** Columns this metric's own source returns (fact table, or the SQL's tables). */
  columnChoices: string[]
  /** Where `columnChoices` came from, for the empty-state hint. */
  columnSource: 'fact' | 'preview' | 'schema'
}

/**
 * Anomaly detection plus the dimension columns (breakdowns, app version,
 * platform). Event-composition metrics have no warehouse source, so only the
 * toggle renders for them.
 *
 * The dimensions are optional and most metrics need none, so they sit behind
 * a collapsed "Breakdowns and dimensions" row that says what is set.
 * A metric that already has some opens with them shown.
 */
export function MonitoringFields({ draft, patch, columnChoices, columnSource }: MonitoringFieldsProps) {
  const warehouseBacked = draft.kind !== 'event_composition'
  // Columns ticked that this metric's source does not return — typically kept
  // from another kind, or from before the query changed. Still sent, and would
  // fail at collection, so they are named rather than rendered as mystery
  // chips. Only once there is a list to compare against.
  const known = new Set(columnChoices)
  const unknown =
    columnChoices.length > 0 ? draft.breakdownColumns.filter(column => !known.has(column)) : []
  const breakdownHint =
    columnChoices.length === 0 && draft.kind === 'sql'
      ? 'Run Preview to list the columns your query returns, then tick the ones to break this metric down by.'
      : columnSource === 'preview'
        ? 'Columns the last preview returned. Tick the columns to break this metric down by.'
        : 'Columns to roll up by. Tick the columns to break this metric down by.'
  const [open, setOpen] = useState(
    draft.breakdownColumns.length > 0 || !!draft.appVersionColumn || !!draft.platformColumn,
  )
  const setCount =
    draft.breakdownColumns.length + (draft.appVersionColumn ? 1 : 0) + (draft.platformColumn ? 1 : 0)
  const summary = [
    setCount === 0 ? 'None set' : setCount === 1 ? '1 set' : `${setCount} set`,
    unknown.length > 0 ? `${unknown.length} not returned by the source` : null,
  ]
    .filter(Boolean)
    .join(' · ')

  return (
    <SCard title="Monitoring" description="Anomaly detection and dimensional breakdowns.">
      <ToggleRow
        label="Anomaly detection"
        hint="Learn a baseline and flag spikes/drops on this metric."
        value={draft.anomalyDetection}
        onChange={value => patch({ anomalyDetection: value })}
        // The dimensions row below draws its own top border.
        last
      />
      {warehouseBacked && (
        <Collapsible open={open} onOpenChange={setOpen}>
          <CollapsibleTrigger asChild>
            <button
              type="button"
              className="flex w-full items-center gap-2 px-4 py-[13px] text-left transition-colors hover:bg-[var(--surface-hover)] border-t border-border-subtle"
            >
              <ChevronRight
                size={14}
                aria-hidden="true"
                className="shrink-0 transition-transform text-fg-tertiary"
                style={{ transform: open ? 'rotate(90deg)' : undefined }}
              />
              <span className="text-body font-medium text-fg">
                Breakdowns and dimensions
              </span>
              <span className="text-body-sm text-fg-tertiary">
                (optional)
              </span>
              <span
                className="ml-auto text-caption"
                style={{ color: unknown.length > 0 ? 'var(--warning)' : 'var(--fg-subtle)' }}
              >
                {summary}
              </span>
            </button>
          </CollapsibleTrigger>
          <CollapsibleContent style={{ borderTop: '1px solid var(--border-subtle)' }}>
            {/* A grid of individually-labelled checkboxes: nothing a <label> can
                point at, so the row names the group. */}
            <Field label="Breakdown columns" htmlFor={false} hint={breakdownHint}>
              <div className="max-w-[560px]">
                <ColumnCheckboxPicker
                  id="metric-breakdowns"
                  columns={columnChoices}
                  value={draft.breakdownColumns}
                  onChange={value => patch({ breakdownColumns: value })}
                  reserved={[draft.appVersionColumn, draft.platformColumn].filter(Boolean)}
                />
              </div>
              {unknown.length > 0 && (
                <p className="mt-[6px] text-body-sm leading-[1.45]" style={{ color: 'var(--warning, var(--fg-muted))' }}>
                  Not returned by this metric's source: {unknown.join(', ')}. Untick them, or
                  collection will fail for them.
                </p>
              )}
            </Field>
            <Field label="App version column" htmlFor="metric-app-version" hint="Optional column used for by-version series.">
              <div className="max-w-[280px]">
                <ColumnSuggestInput
                  id="metric-app-version"
                  value={draft.appVersionColumn}
                  onChange={value => patch({ appVersionColumn: value })}
                  suggestions={columnChoices}
                  placeholder={examplePlaceholder('app_version')}
                />
              </div>
            </Field>
            <Field label="Platform column" htmlFor="metric-platform" last hint="Optional platform dimension column.">
              <div className="max-w-[280px]">
                <ColumnSuggestInput
                  id="metric-platform"
                  value={draft.platformColumn}
                  onChange={value => patch({ platformColumn: value })}
                  suggestions={columnChoices}
                  placeholder={examplePlaceholder('platform')}
                />
              </div>
            </Field>
          </CollapsibleContent>
        </Collapsible>
      )}
    </SCard>
  )
}
