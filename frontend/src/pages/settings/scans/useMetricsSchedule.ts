import { useQuery } from '@tanstack/react-query'
import { scansApi } from '@/api/scans'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { scanConfigKey } from '@/lib/queryKeys'
import type { ScanConfig } from '@/types'
import { scanModeOf } from './scanMode'
import type { MetricsSchedule } from './scanUtils'

/**
 * A monitoring scan's metrics schedule from `GET /scans/{id}`:
 * the scheduler's own due check, which the job list cannot reproduce. Shares
 * `scanConfigKey` with the monitoring page, which reads the same response.
 * Null for a scan the scheduler never collects (or not loaded yet), and while
 * the read is pending or failed — the callers fall back to the job list then.
 */
export function useMetricsSchedule(
  slug: string,
  scanConfig: ScanConfig | undefined,
): MetricsSchedule | null {
  const monitoring = !!scanConfig && scanModeOf(scanConfig) === 'monitoring'
  const scanConfigId = scanConfig?.id
  const { data } = useQuery({
    queryKey: scanConfigKey(slug, scanConfigId),
    queryFn: () => scansApi.get(slug, scanConfigId!),
    enabled: monitoring && !!scanConfigId,
    // The due moment moves at interval boundaries; a minute keeps "in 3m" honest.
    refetchInterval: 60_000,
    meta: SILENT_ERROR_META,
  })
  if (!monitoring || !data) return null
  return {
    lastRunAt: data.last_metrics_run_at ?? null,
    nextRunAt: data.next_metrics_run_at ?? null,
  }
}
