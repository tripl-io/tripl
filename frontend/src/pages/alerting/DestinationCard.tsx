import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { AlertCircle, CheckCircle2, Loader2, Pencil, Send, Trash2 } from "lucide-react"
import type { AlertDestination } from "@/types"
import { alertingApi } from "@/api/alerting"
import { Chip } from "@/components/primitives/chip"
import { Button } from "@/components/ui/button"
import { IconButton } from "@/components/ui/icon-button"
import { Card, CardContent } from "@/components/ui/card"
import { Switch } from "@/components/ui/switch"
import { cn, getErrorMessage } from '@/lib/utils'
import { SILENT_ERROR_META, surfaceError } from "@/lib/errorFeedback"
import { stripValueErrorPrefix } from "@/lib/alertStatus"
import { formatDateTime } from "@/lib/datetime"
import { countOf } from "@/lib/plural"
import { formatInProjectZone } from "./deliverySchedule"
import { ChannelGlyph, channelLabel } from "./channelMeta"
import { describeDeletionImpact } from "./deletionImpact"
import { describeTestFailure, destinationScheduleLabel } from "./destinationCardLabels"
import { invalidateAlertingConfig } from "./alertingCache"

interface DestinationCardProps {
  slug: string
  destination: AlertDestination
  // Threaded from the section rather than read here, so one card cannot show a
  // switch its neighbour hides. Everything it guards is an editor-only endpoint
  // (deps.py `require_editor`).
  canWrite: boolean
  onEditDestination: (destination: AlertDestination) => void
  /** Delete, confirmed by the page. Absent where the section offers none. */
  onDeleteDestination?: (destination: AlertDestination) => void
  /** This destination's delete is in flight: its control is inert (ALR-6). */
  isDeleting?: boolean
}

/**
 * One channel: what it is, whether it is wired up, and how much has gone
 * through it.
 *
 * The rules that route to this destination used to be rendered, edited and
 * deleted inside this card. They now live in the Monitors section,
 * which is also where their live firing state is — the state
 * this card never had, and the whole reason a second screen existed to show
 * it. The card keeps the rule COUNT, because "wired up and nothing routes
 * here" is a fact about the destination.
 */
export function DestinationCard({
  slug,
  destination,
  canWrite,
  onEditDestination,
  onDeleteDestination,
  isDeleting = false,
}: DestinationCardProps) {
  const qc = useQueryClient()

  // Goes through the one shared invalidation: a destination write also moves
  // the Inbox and the delivery log, and eight hand-kept copies of
  // `['alertDestinations', slug]` is how none of them did.
  //
  // A refusal says why. The switch snaps back to the server's value either
  // way, and on its own that read as a click that did nothing — a 403 after a
  // demotion, or the API refusing to enable a demo's Slack example (ALR-6).
  const updateDestinationMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (data: { enabled?: boolean }) =>
      alertingApi.updateDestination(slug, destination.id, data),
    onSuccess: () => invalidateAlertingConfig(qc, slug),
    onError: error => {
      surfaceError(error, stripValueErrorPrefix)
    },
  })

  // A test send is deliberately NOT invalidating the destinations list: the
  // backend records it in the audit log rather than as an AlertDelivery, so the
  // counts on this card do not move and a refetch would only throw away the
  // answer the operator is reading.
  //
  // `testedVersion` is the channel's configuration fingerprint when the test
  // was sent: a result describes the settings stored THEN. After a change it is
  // hidden rather than left saying "the channel refused… Unauthorized" under a
  // token the operator has just replaced (ALR-40).
  //
  // Not `updated_at`: that moves on every write to the row, including the
  // digest flusher's `last_flushed_at` on each cadence tick and the Enabled
  // switch, so a result vanished at the next refetch with nothing changed. A
  // replaced secret does not move the fingerprint (the `*_set` flag stays
  // true), so opening the editor drops the result too.
  const [testedVersion, setTestedVersion] = useState<string | null>(null)
  const testDestinationMut = useMutation({
    // Its outcome renders on the card, transport failure included.
    meta: SILENT_ERROR_META,
    mutationFn: () => alertingApi.testDestination(slug, destination.id),
  })

  const testIsCurrent = testedVersion === destinationConfigFingerprint(destination)
  const testResult = testIsCurrent ? testDestinationMut.data ?? null : null
  const testFailed = testIsCurrent && testDestinationMut.isError

  // Whether any write-only credential is stored. One muted "Configured" says
  // it, where four "webhook set" / "bot token set" / "url set" pills did
  // (AL-24); which value is stored is not something the card can show anyway.
  const hasStoredCredential =
    destination.webhook_set
    || destination.bot_token_set
    || destination.target_url_set
    || destination.jira_api_token_set
    || destination.linear_api_key_set
  const refusal = testResult && !testResult.ok ? describeTestFailure(testResult.error, testResult) : null
  const testTone = testDestinationMut.isPending
    ? 'pending'
    : testResult?.ok
      ? 'ok'
      : 'failed'

  return (
    // A disabled destination reads as muted — the switch is the state, so a
    // solid "enabled" pill beside it only drew the eye to the least important
    // fact on the card (AL-24).
    <Card className={cn(!destination.enabled && 'bg-bg-sunken')}>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          {/* `min-w-0` + `flex-wrap` on the name row: at 390px the row used to
              clip its own tail, and the tail is the chat id — the only value
              that says WHICH Telegram chat this destination points at.
              */}
          <div className="min-w-0 flex-1 space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              {/* The channel as its icon and its name, not the raw uppercase
                  enum (`WEBHOOK`, `DEMO_SINK`) it used to be (AL-24). */}
              <ChannelGlyph
                type={destination.type}
                aria-hidden="true"
                className="size-4 shrink-0 text-fg-tertiary"
              />
              <span className="font-semibold">{destination.name}</span>
              <span className="text-body-sm text-fg-tertiary">{channelLabel(destination.type)}</span>
              {!destination.enabled && <Chip size="xs">Disabled</Chip>}
              {destination.is_local && (
                <Chip variant="outline">
                  local · nothing is sent
                </Chip>
              )}
              {destination.type === 'telegram' && destination.chat_id && (
                <Chip variant="outline" className="h-auto min-h-5 max-w-full whitespace-normal break-all py-0.5">
                  chat {destination.chat_id}
                </Chip>
              )}
              {destination.type === 'webhook' && destination.webhook_header_name && (
                <Chip variant="outline">header {destination.webhook_header_name}</Chip>
              )}
            </div>
            {destination.delivery_schedule_cron && (
              <p
                className="text-body-sm text-fg-tertiary"
                title={
                  destination.next_digest_at
                    ? `Next digest ${formatInProjectZone(destination.next_digest_at, destination.project_timezone)}`
                    : undefined
                }
              >
                {destinationScheduleLabel(
                  destination.delivery_schedule_cron,
                  destination.project_timezone,
                  destination.held_count,
                )}
              </p>
            )}
            {/* Traffic, not just configuration. A destination that has carried
                nothing looks identical to a working one everywhere else on
                this card, and the two are opposite facts. The
                rule count stays after the rules themselves moved to Monitors:
                "enabled, wired up, and nothing routes here" is a state worth
                reading off the channel. */}
            <p className="text-body-sm text-fg-tertiary">
              <span>
                {countOf(destination.rules.length, 'rule', 'rules')}
                {' · '}
                {countOf(destination.delivery_count, 'delivery', 'deliveries')}
                {' · '}
                {countOf(destination.incident_count, 'incident', 'incidents')}
              </span>
              {hasStoredCredential && (
                <>
                  {' · '}
                  <span>Configured</span>
                </>
              )}
            </p>
          </div>
          {/* The whole control cluster goes for a viewer — a test send puts a
              message in somebody's Slack, and the switch and the pencil are
              both 403s. The card keeps every fact it was showing. */}
          {canWrite && (
          <div className="flex shrink-0 items-center gap-2">
            {/* "Configured" and a chat id mean a value is STORED. A revoked
                token stores exactly as well as a live one, so the only way to
                answer "did I actually wire this up?" is to push a message
                through the real channel. */}
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                setTestedVersion(destinationConfigFingerprint(destination))
                testDestinationMut.mutate()
              }}
              disabled={testDestinationMut.isPending}
              aria-label={`Send a test message through ${destination.name}`}
            >
              <Send aria-hidden="true" />
              {testDestinationMut.isPending ? 'Sending…' : 'Test'}
            </Button>
            {/* `checked` is the server's value, and the control is inert while
                its own write is in flight, so a second click cannot queue a
                write against a state that has not landed yet. */}
            <Switch
              checked={destination.enabled}
              disabled={updateDestinationMut.isPending}
              onCheckedChange={checked => updateDestinationMut.mutate({ enabled: checked })}
              aria-label={`Toggle ${destination.name}`}
            />
            <IconButton variant="ghost" label={`Edit destination ${destination.name}`} onClick={() => {
              // The editor can replace a secret the fingerprint cannot see.
              setTestedVersion(null)
              onEditDestination(destination)
            }}>
              <Pencil aria-hidden="true" className="size-4" />
            </IconButton>
            {/* Inside the card, beside Edit, like the Monitors rule rows — it
                used to float under the card, in the gap before the next one,
                where it was unclear which card it deleted (AL-25). The
                confirm itself lives on the page that owns the delete mutation
                and states the same cascade through the same helper; the
                tooltip repeats it on the control, for a reader still deciding
                whether to press it. Absent where the section
                offers no delete (a demo's local sink). */}
            {onDeleteDestination && (
              <IconButton
                variant="ghost"
                className="text-fg-tertiary hover:text-destructive"
                label={`Delete destination ${destination.name}`}
                tooltip={`Deletes "${destination.name}", its rules, and their history. ${describeDeletionImpact(destination.delivery_count, destination.incident_count)}`}
                disabled={isDeleting}
                onClick={() => onDeleteDestination(destination)}
              >
                {isDeleting ? (
                  <Loader2 aria-hidden="true" className="size-4 animate-spin" />
                ) : (
                  <Trash2 aria-hidden="true" className="size-4" />
                )}
              </IconButton>
            )}
          </div>
          )}
        </div>

        {/* A channel refusal arrives as a 200 with `ok: false` — it is the
            answer the button was pressed for, so it renders as a result and
            not as a crash. Only a transport failure gets `role="alert"`.
            One inline row with an icon and its own Dismiss, rather than a
            sentence alone in a tall card with a far-away button (AL-30). */}
        {(testDestinationMut.isPending || testResult || testFailed) && (
          <div
            data-tone={testTone}
            className={cn(
              'flex items-start gap-2 rounded-control border px-3 py-2 text-body-sm',
              testTone === 'ok' && 'border-success/40 bg-success-soft',
              testTone === 'failed' && 'border-destructive/40 bg-danger-soft',
            )}
          >
            {testTone === 'pending' ? (
              <Loader2 aria-hidden="true" className="mt-0.5 size-4 shrink-0 animate-spin text-fg-tertiary" />
            ) : testTone === 'ok' ? (
              <CheckCircle2 aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-success" />
            ) : (
              <AlertCircle aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-destructive" />
            )}
            <div className="min-w-0 flex-1 space-y-1">
              <p
                role={testFailed ? 'alert' : 'status'}
                className={
                  testTone === 'ok'
                    ? 'text-success'
                    : testTone === 'pending'
                      ? 'text-fg-tertiary'
                      : 'text-destructive'
                }
              >
                {testDestinationMut.isPending && 'Sending a test message…'}
                {!testDestinationMut.isPending && testResult?.ok && (
                  testResult.sent_at
                    ? `Test message reached the channel at ${formatDateTime(testResult.sent_at)}.`
                    : 'Test message reached the channel.'
                )}
                {!testDestinationMut.isPending && refusal && (
                  refusal.detail
                    ? `Test message not delivered. ${refusal.summary}`
                    : `The channel refused the test message: ${refusal.summary}`
                )}
                {!testDestinationMut.isPending && !testResult && testFailed && (
                  `Test send failed: ${getErrorMessage(testDestinationMut.error)}`
                )}
              </p>
              {/* The transport's own words, for whoever has to fix the proxy
                  or the firewall — kept, just not as the headline. */}
              {!testDestinationMut.isPending && refusal?.detail && (
                <details className="text-caption text-fg-tertiary">
                  <summary className="cursor-pointer">Details</summary>
                  <p className="mt-1 whitespace-pre-wrap break-words font-mono">{refusal.detail}</p>
                </details>
              )}
            </div>
            {/* A result stays until the channel's settings change, so the
                reader can put it away once it has been read. */}
            {!testDestinationMut.isPending && (
              <Button
                type="button"
                variant="ghost"
                size="xs"
                className="shrink-0"
                onClick={() => setTestedVersion(null)}
                aria-label={`Dismiss the test result for ${destination.name}`}
              >
                Dismiss
              </Button>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

/**
 * What a test send was a test OF: the channel type, which secrets are stored,
 * and every non-secret delivery setting. Deliberately leaves out `updated_at`,
 * `enabled`, the schedule and the digest bookkeeping, none of which change
 * whether the channel accepts a message.
 */
function destinationConfigFingerprint(destination: AlertDestination): string {
  return JSON.stringify([
    destination.type,
    destination.webhook_set,
    destination.bot_token_set,
    destination.chat_id,
    destination.target_url_set,
    destination.webhook_header_name,
    destination.email_recipients,
    destination.email_from_address,
    destination.email_subject_template,
    destination.jira_base_url,
    destination.jira_auth_email,
    destination.jira_api_token_set,
    destination.jira_project_key,
    destination.jira_issue_type,
    destination.linear_api_key_set,
    destination.linear_team_id,
    destination.linear_state_id,
    destination.linear_label_ids,
  ])
}
