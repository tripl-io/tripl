import { useState } from 'react'
import { Link } from 'react-router-dom'
import { MiniStat, MiniStatStrip } from '@/components/primitives/mini-stat'
import { Button } from '@/components/ui/button'
import type { ScanJob } from '@/types'
import { formatDateTime } from '@/lib/datetime'
import { friendlyScanError } from '@/lib/scanError'
import { useIsOwner } from '@/lib/permissions'
import { useExpandedSignals } from '@/hooks/useExpandedSignals'
import { ReplayChunkProgress } from './ReplayChunkProgress'
import { ScanErrorTechnicalDetails } from './ScanErrorTechnicalDetails'
import type { ScanMode } from './scanMode'
import { buildRunReport, type RunReportLine, type RunReportTarget } from './runReport'
import { scanErrorNextStep } from './scanErrorNextStep'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

/**
 * Where a report line's counter can be inspected. Both filter by SCAN, not by
 * run: `result_summary` stores counts only, with no signal or delivery ids, so
 * the run cannot be reconstructed from it. The titles say "from this scan" so
 * the copy does not imply otherwise.
 */
const TARGET_LINK: Record<
  RunReportTarget,
  { href: (slug: string, scanConfigId: string) => string; title: string }
> = {
  anomalies: {
    href: (slug, scanConfigId) => projectPath(currentOrgSlug(), slug, `/anomalies?scan=${scanConfigId}`),
    title: 'View anomalies from this scan',
  },
  alerts: {
    href: (slug, scanConfigId) => projectPath(currentOrgSlug(), slug, `/alerting?scan=${scanConfigId}`),
    title: 'View alerts from this scan',
  },
}

/**
 * A counter that names something reachable links to the surface that holds it;
 * a zero renders as plain text. A link to a guaranteed-empty page is worse than
 * no link, and 0 is guaranteed-empty by construction.
 */
function CounterValue({
  value,
  target,
  slug,
  scanConfigId,
  color,
}: {
  value: number
  target: RunReportTarget
  slug: string
  scanConfigId: string
  color: string
}) {
  // Sized by the MiniStat it sits in; only the tone is its own.
  const body = <span style={{ color }}>{value}</span>
  if (value <= 0) return body
  const link = TARGET_LINK[target]
  return (
    <Link to={link.href(slug, scanConfigId)} title={link.title} className="no-underline hover:underline">
      {body}
    </Link>
  )
}

function RunReportSentence({
  line,
  slug,
  scanConfigId,
}: {
  line: RunReportLine
  slug: string
  scanConfigId: string
}) {
  const link = line.target ? TARGET_LINK[line.target] : null
  return (
    <li className="text-body leading-relaxed text-foreground">
      {link ? (
        <Link
          to={link.href(slug, scanConfigId)}
          title={link.title}
          className="text-foreground no-underline hover:underline"
        >
          {line.text}
        </Link>
      ) : (
        <span title={line.title}>{line.text}</span>
      )}
      {line.hint && (
        <span className="mt-0.5 block text-caption leading-snug text-fg-tertiary">
          {line.hint}
        </span>
      )}
    </li>
  )
}

/**
 * The expanded body of one scan run.
 *
 * It leads with "What this run did" — plain sentences about the user's data —
 * and keeps the raw counters behind "Show raw counters" for the operator who
 * wants them: the eight cards that used to BE this panel, plus the scheduled
 * sampler sweep (paths sampled / with samples, values written, contexts
 * unfilled). Only json_path_ring_size stays summary-only — the ring is an
 * internal pacing detail the unfilled count already narrates. Internal
 * counters answered no question anyone had; "did my events arrive, and which
 * ones" is the question, and it now has an answer above the fold.
 */
export function JobDetails({
  job,
  slug,
  scanConfigId,
  mode,
  dataSourceId,
}: {
  job: ScanJob
  slug: string
  scanConfigId: string
  /** Derived from the config, not the run — it decides the catalog-only line. */
  mode: ScanMode
  /** The scan's source, so a failure can link an owner to its connection. */
  dataSourceId?: string | null
}) {
  const [countersOpen, setCountersOpen] = useState(false)
  const isOwner = useIsOwner()
  const summary = job.result_summary

  // "Raised N anomaly signals" links to the Anomalies page, which answers a
  // different question: what is OPEN for this scan now. Rather than a permanent
  // paragraph warning that the two numbers may not match, the report puts the
  // other number next to this one — and only when they actually differ.
  //
  // Free in practice: the top bar mounts this exact query on every project page,
  // so it is a cache read, and `enabled` keeps a run that raised nothing from
  // asking at all.
  const signalsAdded = summary?.signals_added ?? 0
  const openSignalsQuery = useExpandedSignals(slug, { enabled: signalsAdded > 0 })
  const openSignals = openSignalsQuery.data
    // Muted and expected signals are out of every open count (MO-4 / JR-5).
    ? openSignalsQuery.data.filter(signal => signal.scan_config_id === scanConfigId && !signal.hidden).length
    : null

  const report = buildRunReport(job, mode, openSignals)
  const error = job.error_message ? friendlyScanError(job.error_message) : null
  const nextStep = error ? scanErrorNextStep(error.message, dataSourceId, isOwner) : null

  return (
    <div className="space-y-3 bg-muted/30 p-4">
      <h4 className="text-body-sm font-semibold uppercase tracking-wide text-fg-tertiary">Run details</h4>
      {error && (
        <div className="rounded-lg border border-destructive/20 bg-destructive/10 p-3 text-body-sm text-destructive">
          {error.message}
          {/* The diagnosis, then what to do about it (#247 DA-20). */}
          {nextStep && (
            <div className="mt-2 space-y-2 text-foreground">
              <p className="m-0">{nextStep.text}</p>
              {nextStep.actions.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {nextStep.actions.map(action => (
                    <Button key={action.label} asChild size="xs" variant="outline">
                      <Link to={action.to}>{action.label}</Link>
                    </Button>
                  ))}
                </div>
              )}
            </div>
          )}
          <ScanErrorTechnicalDetails technical={error.technical} />
        </div>
      )}
      {summary && (
        <>
          {(summary.time_from || summary.time_to) && (
            <div className="rounded-md border bg-background p-3 text-body-sm text-fg-tertiary">
              <span className="font-medium text-foreground">
                {summary.mode === 'metrics_replay' ? 'Replay period' : 'Collection period'}
              </span>
              {summary.time_from && summary.time_to && (
                <span> · {formatDateTime(summary.time_from)} - {formatDateTime(summary.time_to)}</span>
              )}
            </div>
          )}
          <ReplayChunkProgress summary={summary} />
          {report.length > 0 && (
            <div className="rounded-md border bg-background p-3">
              <h5 className="mb-1.5 text-body-sm font-semibold text-foreground">What this run did</h5>
              <ul className="m-0 list-none space-y-1 p-0">
                {report.map(line => (
                  <RunReportSentence key={line.id} line={line} slug={slug} scanConfigId={scanConfigId} />
                ))}
              </ul>
            </div>
          )}
          <div>
            <button
              type="button"
              onClick={() => setCountersOpen(open => !open)}
              aria-expanded={countersOpen}
              className="text-body-sm font-medium text-fg-tertiary hover:underline"
            >
              {countersOpen ? 'Hide raw counters' : 'Show raw counters'}
            </button>
          </div>
          {countersOpen && (
            // The one KPI strip (DS-5) instead of a grid of centred Card tiles
            // with 18px bold figures.
            <MiniStatStrip boxed>
              <MiniStat label="Events created" value={summary.events_created ?? 0} valueTone="success" />
              <MiniStat label="Properties created" value={summary.variables_created ?? 0} valueTone="info" />
              <MiniStat label="Events skipped" value={summary.events_skipped ?? 0} />
              <MiniStat label="Columns analyzed" value={summary.columns_analyzed ?? 0} valueTone="accent" />
              {summary.breakdown_event_metrics != null && (
                <MiniStat label="Event breakdowns" value={summary.breakdown_event_metrics} />
              )}
              {summary.distribution_drifts != null && (
                <MiniStat label="Distribution rows" value={summary.distribution_drifts} />
              )}
              {summary.json_paths_sampled != null && (
                <MiniStat label="Paths sampled" value={summary.json_paths_sampled} />
              )}
              {/* Sampled high with zero coming back is the signature of a
                  failing adapter (the sampler swallows its errors so the run
                  still completes) — the pair has to be visible together. */}
              {summary.json_paths_with_samples != null && (
                <MiniStat label="Paths with samples" value={summary.json_paths_with_samples} />
              )}
              {summary.variable_values_written != null && (
                <MiniStat label="Values written" value={summary.variable_values_written} />
              )}
              {summary.variable_contexts_unfilled != null && (
                <MiniStat label="Contexts unfilled" value={summary.variable_contexts_unfilled} />
              )}
              {/* Reads next to Variables created on purpose: a scheduled run now
                  both mints and retires, and the pair is the only way to tell a
                  catalog that is growing from one holding steady. */}
              {summary.variables_retired != null && (
                <MiniStat label="Properties retired" value={summary.variables_retired} />
              )}
              {summary.signals_added != null && (
                <MiniStat
                  label="Signals added"
                  value={
                    <CounterValue
                      value={summary.signals_added}
                      target="anomalies"
                      slug={slug}
                      scanConfigId={scanConfigId}
                      color="var(--danger)"
                    />
                  }
                />
              )}
              {summary.alerts_queued != null && (
                <MiniStat
                  label="Alerts queued"
                  value={
                    <CounterValue
                      value={summary.alerts_queued}
                      target="alerts"
                      slug={slug}
                      scanConfigId={scanConfigId}
                      color="var(--warning)"
                    />
                  }
                />
              )}
            </MiniStatStrip>
          )}
          {summary.details && summary.details.length > 0 && (
            <div>
              <h5 className="mb-1 text-body-sm font-semibold text-fg-tertiary">Log</h5>
              <div className="max-h-48 overflow-y-auto rounded-lg border bg-background p-2">
                {summary.details.map((detail, i) => (
                  <div key={i} className="mono border-b border-border/50 py-0.5 text-body-sm text-fg-tertiary last:border-0">{detail}</div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
