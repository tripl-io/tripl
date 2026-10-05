import { useQuery } from '@tanstack/react-query'
import { serviceSettingsApi } from '@/api/serviceSettings'
import { InfoRow, SCard } from '@/components/settings/kit'
import { formatDateTime } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { telemetryStatusKey } from '@/lib/queryKeys'

const REASON: Record<string, string> = {
  disabled: 'Off (TELEMETRY_ENABLED is not set)',
  'no endpoint': 'Off (TELEMETRY_ENDPOINT is empty)',
  'public demo': 'Off (a public demo never sends it)',
}

/**
 * Settings › Platform › Runtime: the opt-in usage ping (C2), read-only. Whether
 * it is sent and where, and the exact document it last sent — what
 * website/docs/run/telemetry.md lists, nothing more.
 */
export function TelemetryCard() {
  const query = useQuery({
    queryKey: telemetryStatusKey(),
    queryFn: () => serviceSettingsApi.telemetry(),
    meta: SILENT_ERROR_META,
  })
  const status = query.data
  if (!status) return null
  return (
    <SCard
      title="Usage telemetry"
      description="One anonymous ping a day: no names, emails, hosts or data. Set in the server's environment."
    >
      <InfoRow
        label="Sending"
        value={status.enabled ? 'On' : (REASON[status.reason ?? ''] ?? 'Off')}
        mono={false}
      />
      <InfoRow label="Endpoint" value={status.endpoint || '—'} />
      <InfoRow
        label="Last sent"
        value={
          status.last_attempt_at
            ? `${formatDateTime(status.last_attempt_at)}${status.last_delivered ? '' : ' (not delivered)'}`
            : 'Never'
        }
        mono={false}
        last={!status.last_payload}
      />
      {status.last_payload && (
        <InfoRow
          label="Last payload"
          value={
            <pre className="m-0 max-w-full overflow-x-auto whitespace-pre text-body-sm" data-testid="telemetry-payload">
              {JSON.stringify(status.last_payload, null, 2)}
            </pre>
          }
          last
        />
      )}
    </SCard>
  )
}
