import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { AlertTriangle } from 'lucide-react'
import { Chip } from '@/components/primitives/chip'
import { PageHeader } from '@/components/primitives/page-header'
import { Button } from '@/components/ui/button'
import { WatchButton } from '@/components/watch-button'
import { formatTimestamp } from '@/lib/datetime'
import type { MonitoringScope } from '@/lib/monitoring'
import { getAlertingPath } from '@/lib/navigation'
import { formatRatioDelta, ratioDelta } from '@/lib/percentDelta'
import { DEFAULT_ENTITY_COLOR } from '@/types'
import type {
  EventMetricsResponse,
  EventType,
  MetricDefinitionDetailResponse,
  MonitoringSignal,
} from '@/types'
import { MetricHeaderActions } from './MetricHeaderActions'
import type { MetricCollect } from './useMetricCollect'

/**
 * The header's status chip for the latest signal (MO-12): which way, how far
 * and when ("Spike · +82% at Sep 25, 6:00 PM"), not "Latest scan spike
 * anomaly".
 */
function signalChipLabel(signal: MonitoringSignal): string {
  const word = signal.direction === 'drop' ? 'Drop' : 'Spike'
  if (signal.direction === 'drop' && signal.actual_count === 0) {
    return `Drop to zero at ${formatTimestamp(signal.bucket)}`
  }
  const delta = ratioDelta(signal.actual_count, signal.expected_count)
  return `${word} · ${formatRatioDelta(delta)} at ${formatTimestamp(signal.bucket)}`
}

/**
 * The page header of every scope but the event, which settles into its own
 * hero instead: the title, the scope's identity (the type's dot, the scan's
 * name), the latest signal's chip and, on a catalog metric, its actions.
 */
export function MonitoringDetailHeader({
  slug,
  scope,
  scopeId,
  eyebrow,
  title,
  identity,
  description,
  eventType,
  metrics,
  metricDefinition,
  metricEditPath,
  metricCollect,
  canWrite,
}: {
  slug: string | undefined
  scope: MonitoringScope
  scopeId: string
  eyebrow: string
  title: string
  identity: string | null
  description: ReactNode
  eventType: EventType | undefined
  metrics: EventMetricsResponse | undefined
  metricDefinition: MetricDefinitionDetailResponse | undefined
  metricEditPath: string
  metricCollect: MetricCollect
  canWrite: boolean
}) {
  const latestSignal = metrics?.latest_signal
  // A catalog metric and an event type can be watched (#259); the other scopes
  // (a scan total, a distribution) have nobody to notify about.
  const watchType = scope === 'metric' ? 'metric' : scope === 'event_type' ? 'event_type' : null
  const watch = watchType && slug && scopeId
    ? <WatchButton slug={slug} entityType={watchType} entityId={scopeId} />
    : null
  return (
    <PageHeader
      eyebrow={eyebrow}
      title={title}
      actions={
        scope === 'metric' && canWrite && slug ? (
          <>
            {watch}
            <MetricHeaderActions
              slug={slug}
              scopeId={scopeId}
              metricDefinition={metricDefinition}
              editPath={metricEditPath}
              collect={metricCollect}
            />
          </>
        ) : (watch ?? undefined)
      }
      titleAddon={
        <>
          {identity && (
            <span className="mono text-body text-fg-secondary" data-testid="header-identity">
              {identity}
            </span>
          )}
          {/* No type badge: on an event-type page it repeated the title
              (MO-12). The type's colour is a dot beside it instead. */}
          {scope === 'event_type' && eventType && (
            <span
              aria-hidden="true"
              data-testid="event-type-dot"
              className="inline-block size-2.5 shrink-0 rounded-full"
              style={{ backgroundColor: eventType.color || DEFAULT_ENTITY_COLOR }}
            />
          )}
          {scope === 'project_total' && (metrics?.scan_config_name || metrics?.scan_config_id) && (
            // The scan's name, as the Overview names it; the raw id only
            // on hover (MO-12).
            <span
              className="text-body text-fg-secondary"
              title={metrics.scan_config_id ?? undefined}
              data-testid="header-scan"
            >
              Scan: {metrics.scan_config_name || metrics.scan_config_id?.slice(0, 8)}
            </span>
          )}
          {latestSignal && (
            // The soft status chip every other status uses: a recent
            // signal in the warning tone, the latest scan's in danger.
            <Chip
              tone={latestSignal.state === 'recent' ? 'warning' : 'danger'}
              icon={<AlertTriangle className="size-3" aria-hidden="true" />}
            >
              {signalChipLabel(latestSignal)}
            </Chip>
          )}
          {/* Not a dead end (MO-4): the signal's incident, with its Ack /
              Mute / Resolve, lives in the alert inbox. */}
          {latestSignal && slug && (
            <Button variant="link" size="sm" className="h-auto p-0 text-caption" asChild>
              <Link to={getAlertingPath(slug)}>View alerts</Link>
            </Button>
          )}
        </>
      }
      description={description}
    />
  )
}
