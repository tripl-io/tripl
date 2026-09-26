import { type ReactNode, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ArrowDown, ArrowUp, Bell, BellOff, ChevronDown, ChevronRight, RefreshCw, Settings2 } from 'lucide-react'
import { alertingApi } from '@/api/alerting'
import { InfoRow, Panel } from '@/components/settings/kit'
import { PageContainer } from '@/components/primitives/page-container'
import { PageHeader } from '@/components/primitives/page-header'
import { LoadingState } from '@/components/primitives/loading-state'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { PageSkeleton, QueryErrorState, ReadOnlyNotice } from '@/components/states'
import { FormRow } from '@/components/ui/form-row'
import { Chip } from '@/components/primitives/chip'
import { MiniStat, MiniStatStrip } from '@/components/primitives/mini-stat'
import { formatDateTime, formatRelativeTime } from '@/lib/datetime'
import { formatNumber } from '@/lib/format'
import {
  MUTE_PRESETS,
  muteChoiceName,
  muteUntilIso,
  unmuteName,
  type MutePreset,
} from '@/lib/mutePresets'
import {
  ALERT_DELIVERY_TONE as DELIVERY_TONE,
  MONITOR_STATUS_LABEL as STATUS_LABEL,
  MONITOR_STATUS_TONE as STATUS_TONE,
  signalDirectionColor,
} from '@/lib/statusLexicon'
import { useAdaptiveRefetchInterval } from '@/realtime/streamContext'
import { RULE_SIGNAL_GROUPS, formatCooldown } from './alerting/constants'
import { channelLabel } from './alerting/channelMeta'
import { InertScopeNotice, inertScopeSentence, type DriftScope } from './alerting/InertScopeNotice'
import type { AlertDelivery, MonitorDetail, MonitorFiringScope } from '@/types'
import { useCanWriteProject } from '@/lib/permissions'
import { getScopeMonitoringPath } from '@/lib/monitoring'
import { formatIncidentCount, scopeKindLabel } from '@/lib/alertStatus'
import { formatPercentDelta } from '@/lib/percentDelta'
import { alertDeliveryKey } from '@/lib/queryKeys'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { usePageTitle } from '@/components/shell-chrome-context'
import { invalidateAlertingConfig } from './alerting/alertingCache'
import { monitorDetailKey, monitorHistoryKey } from '@/lib/queryKeys'

export default function MonitorDetailPage() {
  const { slug, monitorId } = useParams<{ slug: string; monitorId: string }>()
  const queryClient = useQueryClient()
  // Mute and retry are editor actions (MON-6); a viewer reads the history.
  const canWrite = useCanWriteProject()

  const monitorKey = useMemo(() => monitorDetailKey(slug, monitorId), [slug, monitorId])
  const historyKey = useMemo(() => monitorHistoryKey(slug, monitorId), [slug, monitorId])
  const refetchInterval = useAdaptiveRefetchInterval({ activeMs: 60_000 })

  const monitorQuery = useQuery({
    queryKey: monitorKey,
    queryFn: () => alertingApi.getMonitor(slug!, monitorId!),
    enabled: !!slug && !!monitorId,
    refetchInterval,
    staleTime: 30_000,
  })

  const historyQuery = useQuery({
    queryKey: historyKey,
    queryFn: () => alertingApi.getMonitorHistory(slug!, monitorId!, { limit: 50 }),
    enabled: !!slug && !!monitorId,
    refetchInterval,
  })

  // The Monitors list, its summary and the destination card's rule all read
  // the same muted_until from their own queries, so writing only this page's
  // cache left them showing the old state until their next refetch (MON-31).
  // Same helper the Monitors-list mute uses (MonitorsSection).
  const onMuteChanged = (data: MonitorDetail) => {
    queryClient.setQueryData(monitorKey, data)
    if (slug) invalidateAlertingConfig(queryClient, slug)
  }
  const muteMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (mutedUntil: string) => alertingApi.muteMonitor(slug!, monitorId!, mutedUntil),
    onSuccess: onMuteChanged,
  })
  const unmuteMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => alertingApi.unmuteMonitor(slug!, monitorId!),
    onSuccess: onMuteChanged,
  })
  const retryMut = useMutation({
    // The failed row says why, right under its Retry button (MON-31).
    meta: SILENT_ERROR_META,
    mutationFn: (deliveryId: string) => alertingApi.retryDelivery(slug!, deliveryId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: historyKey })
      // "Last delivery" in the strip above is read off the monitor itself, and
      // the delivery log lists the same rows.
      void queryClient.invalidateQueries({ queryKey: monitorKey })
      // The delivery log, the Inbox and the "has ever delivered" probe list
      // the same row the retry just changed.
      if (slug) invalidateAlertingConfig(queryClient, slug)
    },
  })
  const retryError =
    retryMut.isError && retryMut.variables
      ? {
          deliveryId: retryMut.variables,
          message: retryMut.error instanceof Error ? retryMut.error.message : 'Retry failed.',
        }
      : null

  const monitor = monitorQuery.data
  usePageTitle(monitor?.rule_name)
  const muteError = muteMut.error ?? unmuteMut.error

  // The list this rule lives in: Alerting, on its Rules section.
  const rulesListPath = `/p/${slug}/alerting?section=monitors`

  if (monitorQuery.isError) {
    // A rule that does not exist is not a failure to retry (#237 SH-33): a
    // deleted rule's link from an old alert lands on "not found" and a way back.
    return (
      <PageContainer>
        <QueryErrorState
          error={monitorQuery.error}
          title="Could not load this alert rule"
          onRetry={() => {
            void monitorQuery.refetch()
          }}
          notFound={{
            title: 'Alert rule not found',
            description: 'It may have been deleted. Its past deliveries are gone with it.',
            back: { to: rulesListPath, label: 'Back to alert rules' },
          }}
          compact
        />
      </PageContainer>
    )
  }

  if (monitorQuery.isLoading || !monitor) {
    // The page's own shape while it loads, not a line of text (#237).
    return <PageSkeleton variant="detail" label="Loading alert rule…" />
  }

  const statusTone = STATUS_TONE[monitor.status]

  return (
    // The eyebrow names the nav group and the collection, as on every Observe
    // detail page, instead of a separate back link above the header (DS-2 /
    // MO-40); the top bar's breadcrumb is the way back.
    <PageContainer>
      <PageHeader
        eyebrow="Observe · Alert rule"
        title={monitor.rule_name}
        // The state belongs to the title, not to a line of its own between the
        // title and the stats (MO-35). No pulsing dot: the chip says it, and
        // "Last fired · still firing" below is the one place that moves (MO-18).
        titleAddon={
          <span className="inline-flex flex-wrap items-center gap-1.5">
            <Chip tone={statusTone} size="sm">
              {STATUS_LABEL[monitor.status]}
            </Chip>
            {!monitor.rule_enabled && (
              <Chip tone="neutral" size="sm">
                Rule off
              </Chip>
            )}
            {monitor.muted && monitor.muted_until && (
              <Chip tone="warning" size="sm" icon={<BellOff aria-hidden="true" className="size-3" />}>
                Muted until {formatDateTime(monitor.muted_until)}
              </Chip>
            )}
          </span>
        }
        actions={
          // Mute and editing are an editor's job; a viewer gets the notice
          // below instead of controls that would 403.
          slug && canWrite ? (
            <>
              <MuteControl
                // The same string the heading shows: the names have to match
                // what the operator just read (tripl-in45).
                ruleName={monitor.rule_name}
                muted={monitor.muted}
                onMute={(ms) => muteMut.mutate(muteUntilIso(ms))}
                onUnmute={() => unmuteMut.mutate()}
                isPending={muteMut.isPending || unmuteMut.isPending}
              />
              <Button asChild variant="outline" size="sm">
                <Link
                  // A monitor IS an alert rule, and rules are edited in the
                  // Rules section of Alerting (tripl-89ps, JR-28). The section
                  // has to be named: without it the link lands on the incident
                  // Inbox, which is the default, and "Edit rule" opens triage.
                  to={rulesListPath}
                  className="no-underline"
                >
                  <Settings2 aria-hidden="true" />
                  Edit rule
                </Link>
              </Button>
            </>
          ) : undefined
        }
      />

      {muteError instanceof Error && (
        <p role="alert" className="m-0 text-caption text-danger">
          {muteError.message}
        </p>
      )}
      {!canWrite && (
        <ReadOnlyNotice>
          Read-only: your account has the viewer role. Muting this rule and
          retrying its deliveries are done by an editor or owner.
        </ReadOnlyNotice>
      )}

      <RecencyStrip monitor={monitor} />

      <FiringNowPanel slug={slug} scopes={monitor.firing_scopes ?? []} />

      <ConfigPanel slug={slug} monitor={monitor} />

      <DestinationPanel slug={slug} monitor={monitor} />

      <FiredHistoryTimeline
        slug={slug}
        items={historyQuery.data?.items ?? []}
        total={historyQuery.data?.total ?? 0}
        isLoading={historyQuery.isLoading}
        isError={historyQuery.isError}
        onRetry={canWrite ? (deliveryId) => retryMut.mutate(deliveryId) : undefined}
        retryingId={retryMut.isPending ? (retryMut.variables ?? null) : null}
        retryError={retryError}
      />
    </PageContainer>
  )
}

function ActionButton({
  icon,
  label,
  ariaLabel,
  onClick,
  disabled,
}: {
  icon: ReactNode
  label: string
  /**
   * Spoken name, when the visible `label` alone does not say what the button
   * acts ON. A mute preset reads "1h" — three of them on one page are three
   * identically-named buttons, and none of them names the thing about to go
   * quiet (tripl-in45).
   *
   * Every caller that sets this keeps the visible `label` as a SUBSTRING of it
   * ("Mute <rule> for 1h" contains "1h"), so speech-input users can still say
   * what they can read — WCAG 2.5.3 Label in Name, which an aria-label that
   * replaced the visible text outright would break.
   */
  ariaLabel?: string
  onClick: () => void
  disabled?: boolean
}) {
  return (
    // The Button primitive's outline look, hover and focus ring (DS-14), not a
    // hand-painted copy of it.
    <Button
      type="button"
      variant="outline"
      size="sm"
      aria-label={ariaLabel}
      onClick={onClick}
      disabled={disabled}
    >
      {icon}
      {label}
    </Button>
  )
}

/**
 * Mute / unmute for one alert RULE.
 *
 * Two things a reader should not have to reconstruct:
 *
 * 1. The durations come from `@/lib/mutePresets`. This page owned a third copy
 *    of the list plus its own `futureIso` resolver while that module existed
 *    for exactly the purpose of there being one — and a private copy is how the
 *    surfaces drift apart again, which is the defect the module was extracted
 *    to fix (tripl-es0f, tripl-oxkt.7). The shared resolver also takes an
 *    injectable `now`, so a test can pin the instant instead of racing it.
 *
 * 2. There is deliberately NO open-ended option here, even though the incident
 *    Inbox has one. `is_rule_muted()` returns false the moment a rule's
 *    `muted_until` is NULL, so a button promising "until I unmute" would write
 *    the value that UN-mutes the rule. The permanent lever on a rule is the
 *    enable/disable switch, not a mute — which is why `MUTE_PRESETS` (durations
 *    only) is imported here and `INBOX_MUTE_CHOICES` is not (tripl-a50u).
 *
 * 3. Every button here is named by what it silences, not only by its own text.
 *    The presets used to be called "1h" / "24h" / "7d" and Unmute just
 *    "Unmute", so a screen reader announced a duration with no hint of WHOSE
 *    alerts stop — and three same-named buttons if a second control ever shares
 *    the page. The wording is no longer lifted from the other two mute
 *    surfaces, it is IMPORTED: `muteChoiceName` and `unmuteName` live next to
 *    `MUTE_PRESETS` in `@/lib/mutePresets`, so the three surfaces cannot
 *    describe one action three ways without the edit landing in the one module
 *    all three read. Copying — which is what "lifted verbatim" used to mean
 *    here — is what tripl-yapg replaced (tripl-in45, tripl-oxkt.7).
 */
function MuteControl({
  ruleName,
  muted,
  onMute,
  onUnmute,
  isPending,
}: {
  /**
   * What the controls name. A monitor IS an alert rule (tripl-89ps), so the
   * noun is the rule's name — the same one the page heading shows and the same
   * one `MonitorsSection`'s row menu takes under this name. It is a prop and
   * not a lookup because nothing else in this component identifies the rule:
   * `muted` and the callbacks are all anonymous.
   */
  ruleName: string
  muted: boolean
  onMute: (ms: number) => void
  onUnmute: () => void
  isPending: boolean
}) {
  if (muted) {
    return (
      <ActionButton
        icon={<Bell className="h-3.5 w-3.5" />}
        label="Unmute"
        ariaLabel={unmuteName(ruleName)}
        onClick={onUnmute}
        disabled={isPending}
      />
    )
  }
  // A header action, "Mute ▾", next to "Edit rule" (MO-35) — the three
  // presets used to float on a line of their own under the title.
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button type="button" variant="outline" size="sm" disabled={isPending}>
          <BellOff aria-hidden="true" />
          Mute
          <ChevronDown aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" sideOffset={6}>
        {MUTE_PRESETS.map((preset: MutePreset) => (
          <DropdownMenuItem
            key={preset.label}
            // This page maps `MUTE_PRESETS`, whose `ms` is `number`, so this
            // call is statically confined to the "for <duration>" branch of
            // `muteChoiceName`. The open-ended phrasing is unreachable from
            // here without importing `INDEFINITE_MUTE` or `INBOX_MUTE_CHOICES`
            // by name, which tripl-a50u forbids on a rule surface:
            // `is_rule_muted()` reads a NULL `muted_until` as NOT MUTED.
            aria-label={muteChoiceName(ruleName, preset)}
            onSelect={() => onMute(preset.ms)}
          >
            For {preset.label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function RecencyStrip({ monitor }: { monitor: MonitorDetail }) {
  const isFiring = monitor.status === 'firing'
  return (
    <MiniStatStrip boxed>
      <MiniStat
        label="Last fired"
        value={monitor.last_anomaly_at ? formatRelativeTime(monitor.last_anomaly_at) : 'never'}
        tone={isFiring ? 'danger' : 'neutral'}
        pulse={isFiring}
        // "1h ago · now" contradicted itself: the value is when it last fired,
        // and the delta only says the state has not cleared since (LIVE-18).
        delta={isFiring ? 'still firing' : undefined}
      />
      <MiniStat
        label="Last notified"
        value={monitor.last_notified_at ? formatRelativeTime(monitor.last_notified_at) : 'never'}
      />
      <MiniStat
        label="Last delivery"
        value={monitor.last_delivery_at ? formatRelativeTime(monitor.last_delivery_at) : 'never'}
        tone={monitor.last_delivery_status === 'failed' ? 'danger' : 'neutral'}
        delta={monitor.last_delivery_status ?? undefined}
      />
      <MiniStat label="Deliveries" value={formatNumber(monitor.total_deliveries)} />
      {/* The tone used to be set with no delta, and MiniStat paints the tone
          on the delta only — so the emphasis never rendered (MON-42). The
          delta now says what the tone is about. */}
      {/* "4 of 4", once: "4 · 4 firing" said the number twice (MO-36). */}
      <MiniStat
        label="Firing scopes"
        value={`${formatNumber(monitor.firing_scope_count)} of ${formatNumber(monitor.active_scope_count)}`}
        valueTone={monitor.firing_scope_count > 0 ? 'danger' : undefined}
        delta="watched"
      />
    </MiniStatStrip>
  )
}

/**
 * Which scopes are firing now, each linking to its drilldown (MO-36).
 *
 * The strip above only counts them; this says which. Not rendered when nothing
 * fires — and `?? []` at the call site keeps a response that predates the field
 * from painting an empty panel.
 */
function FiringNowPanel({ slug, scopes }: { slug?: string; scopes: MonitorFiringScope[] }) {
  if (scopes.length === 0) return null
  return (
    <Panel title="Firing now" subtitle={`${formatNumber(scopes.length)} ${scopes.length === 1 ? 'scope' : 'scopes'}`}>
      <ul className="m-0 list-none p-0">
        {scopes.map((scope) => (
          <FiringScopeRow
            key={`${scope.scope_type}:${scope.scope_ref}:${scope.scan_config_id ?? ''}`}
            slug={slug}
            scope={scope}
          />
        ))}
      </ul>
    </Panel>
  )
}

function FiringScopeRow({ slug, scope }: { slug?: string; scope: MonitorFiringScope }) {
  const path = slug ? getScopeMonitoringPath(slug, scope) : null
  // A null name is a scope the rule has not notified yet (a cooldown or a mute
  // held the first message back), so there is no delivery to borrow it from.
  const name = (
    <>
      <span className="text-fg-tertiary">{scopeKindLabel(scope.scope_type)}</span>{' '}
      {scope.scope_name ?? (
        <span className="text-fg-tertiary">not notified yet</span>
      )}
    </>
  )
  return (
    <li
      className="flex min-h-(--row-h) flex-wrap items-center gap-2 border-b px-4 py-2 text-body-sm last:border-0 border-border-subtle"
    >
      {scope.direction === 'spike' && (
        <ArrowUp aria-label="Spike" role="img" className="size-3.5 shrink-0" style={{ color: signalDirectionColor('spike') }} />
      )}
      {scope.direction === 'drop' && (
        <ArrowDown aria-label="Drop" role="img" className="size-3.5 shrink-0" style={{ color: signalDirectionColor('drop') }} />
      )}
      {path ? (
        <Link to={path} className="min-w-0 truncate no-underline hover:underline text-fg">
          {name}
        </Link>
      ) : (
        <span className="min-w-0 truncate">{name}</span>
      )}
      <span
        className="tnum ml-auto text-caption text-fg-tertiary"
        title={formatDateTime(scope.last_anomaly_bucket)}
      >
        {formatRelativeTime(scope.last_anomaly_bucket)}
      </span>
    </li>
  )
}

/**
 * Spike and drop with the arrows the rest of the module draws, in their
 * direction colours — not black text triangles (MO-37).
 */
function DirectionValue({ monitor }: { monitor: MonitorDetail }) {
  if (!monitor.notify_on_spike && !monitor.notify_on_drop) return <>—</>
  return (
    <span className="inline-flex flex-wrap items-center gap-3">
      {monitor.notify_on_spike && (
        <span className="inline-flex items-center gap-1">
          <ArrowUp aria-hidden="true" className="size-3.5" style={{ color: signalDirectionColor('spike') }} />
          Spike
        </span>
      )}
      {monitor.notify_on_drop && (
        <span className="inline-flex items-center gap-1">
          <ArrowDown aria-hidden="true" className="size-3.5" style={{ color: signalDirectionColor('drop') }} />
          Drop
        </span>
      )}
    </span>
  )
}

interface WatchedScope {
  label: string
  /** Set only on a drift scope the project has no source data for. */
  inert: DriftScope | null
}

/** Which drift scope, if any, a signal-group key is — for the inert marking. */
const DRIFT_SCOPE_BY_KEY: Partial<Record<string, DriftScope>> = {
  include_distribution_drifts: 'distribution_drift',
  include_variable_value_drifts: 'variable_value_drift',
}

function ConfigPanel({ slug, monitor }: { slug?: string; monitor: MonitorDetail }) {
  // `=== false` rather than `!`: a response without the block — an older server,
  // or a fixture that predates it — must read as "no claim", not as an
  // accusation. A missing fact is not a negative one (tripl-wkwv.1).
  const distributionIsInert = monitor.scope_readiness?.distribution_drift === false
  const valueDriftIsInert = monitor.scope_readiness?.variable_value_drift === false

  const inertByScope: Partial<Record<DriftScope, boolean>> = {
    distribution_drift: distributionIsInert,
    variable_value_drift: valueDriftIsInert,
  }
  // Grouped as the rule editor groups them — volume changes, then "Also alert
  // on" (drift detectors, release regressions, source freshness)
  // — and named in the editor's words, so one scope is not
  // "Distribution" there and "Distribution drifts" here (MO-37, AL-38).
  const groups = RULE_SIGNAL_GROUPS.map((group) => ({
    id: group.id,
    label: group.id === 'volume' ? 'Volume' : group.label,
    scopes: group.scopes
      // `?? false`: a response that predates a flag reads as "not watched".
      .filter((scope) => monitor[scope.key] ?? false)
      .map((scope): WatchedScope => {
        const drift = DRIFT_SCOPE_BY_KEY[scope.key] ?? null
        return { label: scope.label, inert: drift && inertByScope[drift] ? drift : null }
      }),
  })).filter((group) => group.scopes.length > 0)

  const inertScopes = groups
    .flatMap((group) => group.scopes.map((scope) => scope.inert))
    .filter((scope): scope is DriftScope => scope !== null)

  return (
    <Panel title="Condition" subtitle="When this rule fires">
      {/* Which scan's anomalies this rule can see at all — the first thing that
          narrows it, so it reads before the direction and thresholds that narrow
          it further. The screen refused to name it before, while the docs told
          the reader to go and check that scan's own drift settings whenever
          `scope_readiness` looked healthy (tripl-wkwv.9). "All scans" is the
          null, spelled out: an empty row would read as a missing value. The
          wording is the rule editor's own option label, not a third phrasing —
          the screen that SETS this value is the one worth agreeing with. */}
      <InfoRow label="Scan" value={monitor.scan_name ?? 'All scans'} mono={false} />
      <InfoRow label="Direction" value={<DirectionValue monitor={monitor} />} mono={false} />
      <InfoRow
        label="Threshold"
        value={monitor.min_percent_delta > 0 ? `≥ ${monitor.min_percent_delta}% change` : 'Any change'}
        mono={false}
      />
      <InfoRow
        label="Min expected"
        value={monitor.min_expected_count > 0 ? `${formatNumber(monitor.min_expected_count)} events` : 'No minimum'}
        mono={false}
      />
      {/* The same rule was described as "360m" here and "6h" on the alerting
          destinations card, so a reader comparing the two screens saw two
          answers for one value. Both sides now go through the one shared
          formatter (tripl-oxkt.18); only the sentence around it differs. */}
      <InfoRow
        label="Cooldown"
        value={`${formatCooldown(monitor.cooldown_minutes)} between alerts`}
        mono={false}
        last
      />
      {/* Stacks below `sm`, like the InfoRows above it (MON-32). */}
      <FormRow
        labelWidth={200}
        captionClassName="@min-[560px]:pt-1"
        className="gap-1 px-4 py-[11px] @min-[560px]:gap-4"
        style={{ borderTop: '1px solid var(--border-subtle)' }}
        caption={
          <span className="text-body-sm text-fg-tertiary">
            Watching
          </span>
        }
      >
        <div className="flex min-w-0 flex-col gap-2">
          {groups.length > 0 ? (
            groups.map((group) => (
              <div key={group.id} className="flex min-w-0 flex-wrap items-center gap-1.5">
                <span className="w-24 shrink-0 text-caption text-fg-tertiary">
                  {group.label}
                </span>
                {group.scopes.map((scope) => (
                  <Chip
                    key={scope.label}
                    tone={scope.inert ? 'warning' : 'neutral'}
                    size="xs"
                    // A chip alone cannot say why it is marked, and the reader
                    // hovering it is asking exactly that. The notice below
                    // carries the same sentence for anyone who never hovers.
                    title={scope.inert ? inertScopeSentence(scope.inert) : undefined}
                  >
                    {scope.label}
                  </Chip>
                ))}
              </div>
            ))
          ) : (
            <span className="text-body-sm text-fg-tertiary">
              No scopes selected
            </span>
          )}
          {inertScopes.map((scope) => (
            <InertScopeNotice
              key={scope}
              slug={slug}
              scope={scope}
              // The verdict above is still the PROJECT's — this only aims the
              // link at the scan the reader was going to have to find anyway
              // (tripl-wkwv.9). `?? undefined` because the prop is optional and
              // a null would defeat its default.
              scanConfigId={monitor.scan_config_id ?? undefined}
            />
          ))}
        </div>
      </FormRow>
    </Panel>
  )
}

function DestinationPanel({ slug, monitor }: { slug?: string; monitor: MonitorDetail }) {
  return (
    <Panel title="Routes to" subtitle="Where firing alerts are delivered">
      <FormRow
        labelWidth={200}
        className="gap-1 px-4 py-[11px] @min-[560px]:items-center @min-[560px]:gap-4"
        style={{ borderBottom: '1px solid var(--border-subtle)' }}
        caption={
          <span className="text-body-sm text-fg-tertiary">
            Destination
          </span>
        }
      >
        <span className="flex min-w-0 items-center gap-2">
          {/* The channel kind is a category tag: an outline chip (DS-6), in
              words rather than the raw `demo_sink` type (AL-11). */}
          <Chip variant="outline" size="xs">
            {channelLabel(monitor.destination_type)}
          </Chip>
          {slug ? (
            <Link
              to={`/p/${slug}/alerting?section=destinations`}
              className="min-w-0 truncate text-body-sm no-underline hover:underline text-fg"
              title={monitor.destination_name}
            >
              {monitor.destination_name}
            </Link>
          ) : (
            <span className="min-w-0 truncate text-body-sm text-fg" title={monitor.destination_name}>
              {monitor.destination_name}
            </span>
          )}
        </span>
      </FormRow>
      <InfoRow
        label="Status"
        value={
          <Chip tone={monitor.destination_enabled ? 'success' : 'danger'} size="xs">
            {monitor.destination_enabled ? 'Enabled' : 'Disabled'}
          </Chip>
        }
        mono={false}
        last={monitor.destination_enabled}
      />
      {!monitor.destination_enabled && (
        <div
          className="flex items-center gap-2 px-4 py-3 text-body-sm bg-danger-soft text-fg-secondary"
        >
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-danger" />
          This destination is disabled — alerts for this rule will not be delivered.
        </div>
      )}
    </Panel>
  )
}

function FiredHistoryTimeline({
  slug,
  items,
  total,
  isLoading,
  isError,
  onRetry,
  retryingId,
  retryError,
}: {
  slug?: string
  items: AlertDelivery[]
  total: number
  isLoading: boolean
  isError: boolean
  /** Omitted for a viewer, whose rows carry no Retry. */
  onRetry?: (deliveryId: string) => void
  retryingId: string | null
  /** The last retry that failed, shown on its own row. */
  retryError: { deliveryId: string; message: string } | null
}) {
  return (
    <Panel title="Fired history" subtitle={total > 0 ? `${total} total` : undefined}>
      {isLoading ? (
        <LoadingState className="px-4 py-6 text-body-sm" />
      ) : isError ? (
        <div className="px-4 py-6 text-body-sm text-fg-tertiary">
          Could not load delivery history.
        </div>
      ) : items.length === 0 ? (
        <div className="px-4 py-6 text-body-sm text-fg-tertiary">
          This rule has not fired yet.
        </div>
      ) : (
        <ul className="m-0 list-none p-0">
          {items.map((delivery) => (
            <DeliveryRow
              key={delivery.id}
              slug={slug}
              delivery={delivery}
              onRetry={onRetry ? () => onRetry(delivery.id) : undefined}
              retrying={retryingId === delivery.id}
              retryError={
                retryError?.deliveryId === delivery.id ? retryError.message : null
              }
            />
          ))}
        </ul>
      )}
    </Panel>
  )
}

function DeliveryRow({
  slug,
  delivery,
  onRetry,
  retrying,
  retryError,
}: {
  slug?: string
  delivery: AlertDelivery
  onRetry?: () => void
  retrying: boolean
  retryError: string | null
}) {
  // "4 matched" opens the four scopes, each linking to its chart (MO-36): the
  // alert used to be a dead end between the message and the anomaly.
  const [expanded, setExpanded] = useState(false)
  const scopesId = `delivery-scopes-${delivery.id}`
  return (
    <li className="min-h-(--row-h) border-b px-4 py-3 last:border-0 border-border-subtle">
      <div className="flex flex-wrap items-center gap-2">
        <Chip tone={DELIVERY_TONE[delivery.status]} size="xs">
          {delivery.status}
        </Chip>
        <Chip variant="outline" size="xs">
          {channelLabel(delivery.channel)}
        </Chip>
        <span className="min-w-0 flex-1 truncate text-body-sm font-medium">{delivery.scan_name}</span>
        {/* A count and a relative time: sans with tabular digits (DS-17). */}
        {delivery.matched_count > 0 ? (
          <button
            type="button"
            className="tnum inline-flex items-center gap-0.5 rounded-sm text-caption hover:underline text-fg-tertiary"
            aria-expanded={expanded}
            aria-controls={expanded ? scopesId : undefined}
            onClick={() => setExpanded((current) => !current)}
          >
            {expanded
              ? <ChevronDown aria-hidden="true" className="size-3" />
              : <ChevronRight aria-hidden="true" className="size-3" />}
            {formatNumber(delivery.matched_count)} matched
          </button>
        ) : (
          <span className="tnum text-caption text-fg-tertiary">
            0 matched
          </span>
        )}
        <span
          className="tnum text-micro text-fg-tertiary"
          title={formatDateTime(delivery.created_at)}
        >
          {formatRelativeTime(delivery.created_at)}
        </span>
        {delivery.status === 'failed' && onRetry && (
          <ActionButton
            icon={<RefreshCw className="h-3 w-3" />}
            label={retrying ? 'Retrying…' : 'Retry'}
            onClick={onRetry}
            disabled={retrying}
          />
        )}
      </div>
      {delivery.status === 'failed' && delivery.error_message && (
        <p className="mt-1.5 text-caption text-danger">
          {delivery.error_message}
        </p>
      )}
      {/* A failed retry used to hand the button back as "Retry" with no word
          about what happened (MON-31). */}
      {retryError && !retrying && (
        <p role="alert" className="mt-1.5 text-caption text-danger">
          Retry failed: {retryError}
        </p>
      )}
      {expanded && slug && <DeliveryScopes id={scopesId} slug={slug} deliveryId={delivery.id} />}
    </li>
  )
}

/** The scopes one delivery matched, each linking to its drilldown (MO-36). */
function DeliveryScopes({ id, slug, deliveryId }: { id: string; slug: string; deliveryId: string }) {
  // The same key the delivery log's expanded row reads, so a delivery opened
  // there costs no second request here.
  const detailQuery = useQuery({
    queryKey: alertDeliveryKey(slug, deliveryId),
    queryFn: () => alertingApi.getDelivery(slug, deliveryId),
    meta: SILENT_ERROR_META,
  })
  if (detailQuery.isPending) {
    return <LoadingState className="mt-2 text-caption" label="Loading matched scopes…" />
  }
  if (detailQuery.isError) {
    return (
      <p id={id} className="mt-2 text-caption text-fg-tertiary">
        Could not load the matched scopes.
      </p>
    )
  }
  const items = detailQuery.data.items
  if (items.length === 0) {
    return (
      <p id={id} className="mt-2 text-caption text-fg-tertiary">
        No per-scope rows were stored for this delivery.
      </p>
    )
  }
  return (
    <ul id={id} className="mt-2 grid list-none gap-1 p-0 pl-4">
      {items.map((item) => {
        const path = getScopeMonitoringPath(slug, item)
        const name = (
          <>
            <span className="text-fg-tertiary">{scopeKindLabel(item.scope_type)}</span>{' '}
            {item.scope_name}
          </>
        )
        return (
          <li key={item.id} className="flex flex-wrap items-center gap-2 text-caption">
            {item.direction === 'spike'
              ? <ArrowUp aria-label="Spike" role="img" className="size-3" style={{ color: signalDirectionColor('spike') }} />
              : <ArrowDown aria-label="Drop" role="img" className="size-3" style={{ color: signalDirectionColor('drop') }} />}
            {path ? (
              <Link to={path} className="min-w-0 no-underline hover:underline text-fg">
                {name}
              </Link>
            ) : (
              <span className="min-w-0">{name}</span>
            )}
            <span className="tnum text-fg-tertiary">
              {formatIncidentCount(item.actual_count)} vs {formatIncidentCount(item.expected_count)}
              {' · '}
              {formatPercentDelta(item.percent_delta, item.expected_count)}
            </span>
          </li>
        )
      })}
    </ul>
  )
}
