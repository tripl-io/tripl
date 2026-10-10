import { useId, type ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { formatNumber, formatShare } from '@/lib/format'
import {
  ATTRIBUTED_SCOPES,
  attributionValueLabel,
  columnRemainder,
  columnScale,
  formatSignedCount,
} from '@/lib/signalAttribution'
import { signalDirectionColor } from '@/lib/statusLexicon'
import type { MonitoringSignal, SignalAttribution, SignalAttributionColumn } from '@/types'

/** Where a value's link goes; null leaves the value as plain text. */
export type AttributionValueHref = (column: string, value: string) => string | null

// The neutral the remainder ("everything else") is drawn in; only the listed
// values carry the up/down colour pair.
const NEUTRAL_BAR = 'var(--fg-tertiary)'

/**
 * The drilldown's "Why" panel (#255), directly under the Signal card: which
 * breakdown values the flagged bucket's change comes from, and the release
 * that reached traffic just before it.
 *
 * The attribution is stored with the anomaly at detection time, headline and
 * release line included, and the panel prints those two sentences verbatim, so
 * it and the alert that went out read the same words and numbers. A volume signal whose
 * scan splits by nothing says so and links to the scan's settings; a signal
 * with no attribution at all (older than the feature, or a scope it does not
 * cover) renders nothing, or a one-line note on a volume scope.
 */
export function WhyChangedPanel({
  signal,
  valueHref,
  scanSettingsHref,
}: {
  signal: MonitoringSignal
  valueHref?: AttributionValueHref
  /** The scan's settings page, for the empty state; null when unknown. */
  scanSettingsHref: string | null
}) {
  const headingId = useId()
  const attribution = signal.attribution ?? null
  const status = signal.attribution_status
  const volumeScope = ATTRIBUTED_SCOPES.has(signal.scope_type)

  let body: ReactNode
  if (attribution) {
    body = <AttributionBody attribution={attribution} signal={signal} valueHref={valueHref} />
  } else if (status === 'no_breakdown_columns') {
    body = (
      <p className="mt-1 text-body-sm text-fg-secondary" data-testid="why-changed-empty">
        No breakdown columns configured
        {scanSettingsHref ? (
          <>
            {' — '}
            <Link to={scanSettingsHref}>add one in scan settings</Link>
          </>
        ) : (
          ' — add one in scan settings'
        )}
        .
      </p>
    )
  } else if (status === 'not_computed' && volumeScope) {
    body = (
      <p className="mt-1 text-body-sm text-fg-tertiary">
        No attribution is stored for this signal (detected before attribution existed, or no breakdown data for
        this scope).
      </p>
    )
  } else {
    return null
  }

  return (
    <section
      aria-labelledby={headingId}
      data-testid="why-changed-panel"
      className="rounded-card border bg-(--surface) px-4 py-3"
    >
      <h2 id={headingId} className="text-body font-semibold text-fg">
        Why it changed
      </h2>
      {body}
    </section>
  )
}

function AttributionBody({
  attribution,
  signal,
  valueHref,
}: {
  attribution: SignalAttribution
  signal: MonitoringSignal
  valueHref?: AttributionValueHref
}) {
  // Both sentences come from the backend verbatim — the same strings the alert
  // carries — so nothing here re-derives a percent or an hour count.
  const headline = attribution.headline ?? null
  const releaseText = attribution.release_line ?? null
  const columns = attribution.columns ?? []
  return (
    <>
      {headline ? (
        <p className="mt-1 text-body-sm text-fg" data-testid="why-changed-headline">
          {headline}
        </p>
      ) : columns.length === 0 ? (
        <p className="mt-1 text-body-sm text-fg-secondary" data-testid="why-changed-headline">
          No breakdown value explains the {signal.direction}.
        </p>
      ) : null}
      {columns.length > 0 && (
        <div className="mt-3 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {columns.map(column => (
            <AttributionColumn
              key={column.column}
              column={column}
              delta={attribution.delta}
              valueHref={valueHref}
            />
          ))}
        </div>
      )}
      {releaseText && (
        <p className="mt-3 text-body-sm text-fg-secondary" data-testid="why-changed-release">
          {releaseText}
        </p>
      )}
    </>
  )
}

function AttributionColumn({
  column,
  delta,
  valueHref,
}: {
  column: SignalAttributionColumn
  delta: number
  valueHref?: AttributionValueHref
}) {
  const headingId = useId()
  const scale = columnScale(column, delta)
  const remainder = columnRemainder(column, delta)
  return (
    <div data-testid="why-changed-column">
      <div className="flex items-baseline justify-between gap-2">
        <h3 id={headingId} className="truncate text-body-sm font-medium text-fg">
          {column.column}
        </h3>
        <span className="shrink-0 text-caption text-fg-tertiary">
          explains {formatShare(column.explained_share)}
        </span>
      </div>
      <ul aria-labelledby={headingId} className="mt-1.5 grid gap-1">
        {(column.values ?? []).map(value => {
          const href = valueHref?.(column.column, value.value) ?? null
          const label = attributionValueLabel(value.value)
          const comparison = `actual ${formatNumber(Math.round(value.actual))} vs expected ${formatNumber(Math.round(value.expected))}`
          return (
            <BarRow
              key={value.value}
              label={label}
              href={href}
              linkLabel={`Open ${column.column} = ${label}`}
              delta={value.delta}
              scale={scale}
              color={signalDirectionColor(value.delta < 0 ? 'drop' : 'spike')}
              title={`${column.column} = ${label}: ${comparison}`}
              srDetail={comparison}
            />
          )
        })}
        {remainder !== 0 && (
          <BarRow
            label="Everything else"
            href={null}
            delta={remainder}
            scale={scale}
            color={NEUTRAL_BAR}
            muted
          />
        )}
      </ul>
    </div>
  )
}

/**
 * One diverging bar: a value's contribution drawn from a shared zero line,
 * left for a fall and right for a rise, its signed count printed beside it
 * so the colour never carries the sign alone.
 */
function BarRow({
  label,
  href,
  linkLabel,
  delta,
  scale,
  color,
  title,
  srDetail,
  muted = false,
}: {
  label: string
  href: string | null
  linkLabel?: string
  delta: number
  scale: number
  color: string
  title?: string
  /** Read to screen readers after the signed count, e.g. "actual 1,880 vs expected 5,000". */
  srDetail?: string
  muted?: boolean
}) {
  const width = scale > 0 ? (Math.abs(delta) / scale) * 50 : 0
  const negative = delta < 0
  return (
    <li className="grid grid-cols-[minmax(0,7rem)_minmax(3rem,1fr)_auto] items-center gap-2 text-body-sm" title={title}>
      <span className={`truncate ${muted ? 'text-fg-tertiary' : 'text-fg-secondary'}`}>
        {href ? (
          <Link to={href} aria-label={linkLabel}>
            {label}
          </Link>
        ) : (
          label
        )}
      </span>
      <span aria-hidden="true" className="relative h-2">
        <span className="absolute -inset-y-0.5 left-1/2 w-px bg-(--border)" />
        {width > 0 && (
          <span
            className="absolute inset-y-0"
            style={{
              width: `${width}%`,
              backgroundColor: color,
              ...(negative
                ? { right: '50%', borderRadius: '4px 0 0 4px' }
                : { left: '50%', borderRadius: '0 4px 4px 0' }),
            }}
          />
        )}
      </span>
      <span className="text-right tabular-nums text-fg-secondary">
        {formatSignedCount(delta)}
        {srDetail && <span className="sr-only">{`, ${srDetail}`}</span>}
      </span>
    </li>
  )
}
