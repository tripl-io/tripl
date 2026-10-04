import { useQuery } from '@tanstack/react-query'
import { serviceSettingsApi, type RowLimitDefaults } from '@/api/serviceSettings'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { formatNumber } from '@/lib/format'
import { rowLimitDefaultsKey } from '@/lib/queryKeys'

/**
 * The organization's real row caps, for the Limits hints (per organization since F20 PR9). Readable by every
 * signed-in user; silent, because the hints fall back to the shipped defaults.
 */
export function useRowLimitDefaults(): RowLimitDefaults | undefined {
  return useQuery({
    queryKey: rowLimitDefaultsKey(),
    queryFn: serviceSettingsApi.rowLimitDefaults,
    staleTime: 5 * 60_000,
    meta: SILENT_ERROR_META,
  }).data
}

/**
 * "Empty uses the organization default: N". With the organization's value in hand the
 * number is exact; without it the hint quotes the shipped default (backend
 * config.py) and says an owner may have changed it (#247).
 */
export function rowCapHint(kind: 'catalog' | 'metrics', defaults: RowLimitDefaults | undefined): string {
  const lead = kind === 'catalog' ? 'Most rows one catalog run reads.' : 'Most rows one metrics run reads.'
  const actual = kind === 'catalog' ? defaults?.scan_row_limit_default : defaults?.metrics_row_limit_default
  if (actual !== undefined) return `${lead} Empty uses the organization default: ${formatNumber(actual)}.`
  const shipped = kind === 'catalog' ? '50,000' : '100,000'
  return `${lead} Empty uses the organization default: ${shipped} unless changed in its settings.`
}
