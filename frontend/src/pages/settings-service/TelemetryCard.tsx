import { useQuery } from '@tanstack/react-query'
import { ExternalLink } from 'lucide-react'
import { serviceSettingsApi } from '@/api/serviceSettings'
import { InfoRow, SCard } from '@/components/settings/kit'
import { formatDateTime } from '@/lib/datetime'
import { DOCS_SITE_URL } from '@/lib/docsSite'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { telemetryStatusKey } from '@/lib/queryKeys'

/** Every field the ping holds, and how to turn it off. */
const TELEMETRY_DOCS_URL =`${DOCS_SITE_URL}/run/telemetry`

const REASON: Record<string, string> = {
  'do not track': 'Off (DO_NOT_TRACK is set)',
  disabled: 'Off (TELEMETRY_ENABLED=false)',
  'enterprise default': 'Off (Enterprise default; TELEMETRY_ENABLED=true turns it on)',
  'public demo': 'Off (a public demo never sends it)',
}

/**
 * Settings › Platform › Runtime: the usage ping (C2), read-only. Whether
 * it is sent and where, and the exact document it last sent — what
 * website/docs/run/telemetry.md lists, nothing more.
 *
 * The description and the docs link are the disclosure: Community sends the
 * ping unless told not to, so they stay on screen when the status cannot be
 * read rather than taking the whole card with them.
 */
export function TelemetryCard() {
  const query = useQuery({
    queryKey: telemetryStatusKey(),
    queryFn: () => serviceSettingsApi.telemetry(),
    meta: SILENT_ERROR_META,
  })
  const status = query.data
  // Nothing before the first answer, so the card does not flash a blank state.
  if (!status && !query.isError) return null
  return (
    <SCard
      title="Usage telemetry"
      description="One anonymous ping a day: no names, emails, hosts or data. To turn it off, set TELEMETRY_ENABLED=false (or DO_NOT_TRACK=1) in the server's environment and restart."
      footer={
        <a
          href={TELEMETRY_DOCS_URL}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 text-body-sm font-medium text-accent no-underline hover:underline"
        >
          What it sends
          <ExternalLink className="size-3.5" aria-hidden="true" />
        </a>
      }
    >
      {status ? (
        <>
          <InfoRow
            label="Sending"
            value={status.enabled ? 'On' : (REASON[status.reason ?? ''] ?? 'Off')}
            mono={false}
          />
          <InfoRow label="Endpoint" value={status.endpoint} />
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
        </>
      ) : (
        <InfoRow label="Sending" value="Unknown (the status could not be read)" mono={false} last />
      )}
    </SCard>
  )
}
