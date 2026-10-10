import { useEffect, useId, useRef, useState, type ChangeEvent, type ReactNode } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { TriangleAlert } from "lucide-react"
import { anomalySettingsApi } from "@/api/anomalySettings"
import type { ProjectAnomalySettings } from "@/types"
import { Button } from "@/components/ui/button"
import { ErrorState } from "@/components/error-state"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { Panel } from "@/components/settings/kit"
import {
  PageSkeleton,
  ReadOnlyDefinition,
  ReadOnlyNotice,
  SectionSkeleton,
} from "@/components/states"
import { PageContainer } from "@/components/primitives/page-container"
import { PageHeader } from "@/components/primitives/page-header"
import { useConfirm } from "@/hooks/useConfirm"
import { DETECTION_OFF_MESSAGE } from "@/pages/alerting/constants"
import { SILENT_ERROR_META } from "@/lib/errorFeedback"
import { useCanWriteProject } from "@/lib/permissions"
import { getErrorMessage } from '@/lib/utils'
import { anomalyScopeOverridesKey, projectAnomalySettingsKey } from '@/lib/queryKeys'
import { HolidayCalendarPanel } from './HolidayCalendarPanel'
import { countOf } from '@/lib/plural'

// How long the "Saved" hint stays up after an autosave lands.
const SAVED_HINT_MS = 2000

/**
 * A numeric detection setting that saves when you finish, not as you type.
 *
 * These inputs used to fire the mutation from `onChange`, so typing "168" into
 * Baseline window persisted 1, then 16, then 168 — and each intermediate value
 * was a live detection setting that a collection running in that window would
 * read. The field is also briefly empty whenever someone selects-all and
 * retypes, and `Number('')` is `0`, which is *valid* for `min_expected_count`
 * and `anomaly_ingestion_settling_minutes` (settling 0 = score immediately, the
 * very behaviour the allowance exists to prevent). Committing on blur/Enter
 * removes the whole class.
 *
 * Local state is seeded from the server value and re-seeded whenever it changes,
 * so an edit made elsewhere still lands here; empty and non-numeric input is
 * dropped and the field snaps back to the last saved value.
 *
 * Three more things an autosave owes the person typing. A value
 * outside `min`/`max` is refused here with the bound named, instead of being
 * sent to earn a 422. A rejected save puts the saved value back: the server
 * value never changed, so the re-seed above never ran and the input kept the
 * refused number beside the error — and a reload then brought the old one back.
 * And a save that lands says so, briefly, because a commit on blur is otherwise
 * invisible.
 */
function NumberSetting({
  id,
  value,
  min,
  max,
  step,
  onCommit,
  describedBy,
}: {
  id: string
  value: number
  min?: number
  max?: number
  step?: string
  onCommit: (value: number) => Promise<unknown>
  /** The field's one-line hint, so a screen reader hears it with the input. */
  describedBy?: string
}) {
  // Re-seed from the server value when it changes, without an effect: this is
  // React's documented "adjust state while rendering" pattern, and it lands the
  // new value in the same pass rather than flashing the stale one first.
  const [draft, setDraft] = useState(String(value))
  const [seeded, setSeeded] = useState(value)
  if (seeded !== value) {
    setSeeded(value)
    setDraft(String(value))
  }
  const [status, setStatus] = useState<'idle' | 'saving' | 'saved'>('idle')
  const [rangeError, setRangeError] = useState<string | null>(null)
  const savedTimer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(savedTimer.current), [])
  const hintId = useId()

  const commit = () => {
    const raw = draft.trim()
    const parsed = Number(raw)
    if (raw === '' || !Number.isFinite(parsed)) {
      setDraft(String(value))
      setRangeError(null)
      return
    }
    if ((min !== undefined && parsed < min) || (max !== undefined && parsed > max)) {
      // The refused number STAYS in the box beside the message. It used
      // to snap back to the saved value while the red border and "Must be at
      // least 0.1." stayed up under it: the reader could not see what had been
      // rejected, and the error sat under a valid number until they typed again.
      // Nothing is committed; typing clears the error.
      setRangeError(
        min !== undefined && max !== undefined
          ? `${raw} is out of range: use ${min} to ${max}. Not saved.`
          : min !== undefined
            ? `${raw} is below the minimum (${min}). Not saved.`
            : `${raw} is above the maximum (${max}). Not saved.`,
      )
      return
    }
    setRangeError(null)
    if (parsed === value) return
    setStatus('saving')
    onCommit(parsed).then(
      () => {
        setStatus('saved')
        window.clearTimeout(savedTimer.current)
        savedTimer.current = window.setTimeout(() => setStatus('idle'), SAVED_HINT_MS)
      },
      () => {
        // The refusal itself is rendered once, under the card.
        setStatus('idle')
        setDraft(String(value))
      },
    )
  }

  return (
    <div className="grid gap-1">
      <Input
        id={id}
        type="number"
        min={min}
        max={max}
        step={step}
        value={draft}
        aria-invalid={rangeError ? true : undefined}
        aria-describedby={[rangeError ? hintId : null, describedBy].filter(Boolean).join(' ') || undefined}
        onChange={(e: ChangeEvent<HTMLInputElement>) => {
          setDraft(e.target.value)
          setRangeError(null)
        }}
        onBlur={commit}
        onKeyDown={e => {
          if (e.key === 'Enter') e.currentTarget.blur()
        }}
      />
      {rangeError ? (
        <p id={hintId} role="alert" className="text-body-sm text-destructive">{rangeError}</p>
      ) : (
        <p role="status" className="min-h-4 text-body-sm text-fg-tertiary">
          {status === 'saving' ? 'Saving…' : status === 'saved' ? 'Saved' : ''}
        </p>
      )}
    </div>
  )
}

const SCOPE_TYPE_LABELS: Record<string, string> = {
  project_total: 'Project total',
  event_type: 'Event type',
  event: 'Event',
  metric: 'Metric',
}

/**
 * The undo surface for the false-positive ratchet.
 *
 * Marking an alert a false positive permanently tightens the scope it fired on
 * — it never decays and there is no confirmation step — so the only way back is
 * to see the override here and remove it. Deleting drops that scope straight
 * back to the project settings above; every other scope is untouched.
 */
function ScopeOverridesCard({ slug, canWrite }: { slug: string; canWrite: boolean }) {
  const qc = useQueryClient()
  const { confirm, dialog } = useConfirm()
  const { data, isPending, isError, error, refetch } = useQuery({
    queryKey: anomalyScopeOverridesKey(slug),
    queryFn: () => anomalySettingsApi.listScopeOverrides(slug),
    // Rendered as an ErrorState in the card.
    meta: SILENT_ERROR_META,
  })

  const removeMut = useMutation({
    // Its error is rendered under the list.
    meta: SILENT_ERROR_META,
    mutationFn: (overrideId: string) => anomalySettingsApi.deleteScopeOverride(slug, overrideId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: anomalyScopeOverridesKey(slug) })
    },
  })

  const overrides = data?.items ?? []

  // Removing an override is not undoable from here either: the scope drops
  // straight back to the project settings, and only more false-positive marks
  // would tighten it again. One click used to do it.
  const handleRemove = async (overrideId: string, scopeName: string) => {
    const ok = await confirm({
      title: 'Remove scope override',
      message: `Remove the override for "${scopeName}"? The scope goes back to the project settings above, and its false-positive count is lost.`,
      confirmLabel: 'Remove',
      variant: 'danger',
    })
    if (ok) removeMut.mutate(overrideId)
  }

  return (
    // A titled section, not a <Label> posing as a heading: the field
    // labels were larger than it, so the hierarchy read upside down.
    <Panel
      title="Scope overrides"
      subtitle="Scopes a false-positive mark made stricter, permanently"
    >
      {dialog}
      <div className="space-y-4 p-4">
        <p className="text-body-sm text-fg-tertiary">
          Marking an alert a <strong>false positive</strong> makes the detector stricter on that
          scope alone — permanently. These overrides replace the sigma threshold and min expected
          count above for the scopes listed. Removing one puts that scope back on the project
          settings.
        </p>

        {/* A failed load is NOT an empty list. `data` is undefined either way, so
            reading the length alone told an operator "no scope has been
            tightened" — a claim about the ratchet — when the request never
            answered. ErrorState is what this app shows for a
            load that failed, and it carries the retry this card needs: it is the
            only undo the permanent ratchet has. */}
        {isPending ? (
          <SectionSkeleton variant="list" rows={2} label="Loading scope overrides…" />
        ) : isError && data === undefined ? (
          <ErrorState
            compact
            title="Couldn't load scope overrides"
            error={error}
            onRetry={() => {
              void refetch()
            }}
            retryLabel="Retry"
          />
        ) : overrides.length === 0 ? (
          <p className="text-body text-fg-tertiary">
            No scope has been tightened. Every scope uses the project settings above.
          </p>
        ) : (
          <ul className="divide-y rounded-lg border">
            {overrides.map(override => (
              <li
                key={override.id}
                className="flex items-center justify-between gap-4 p-3 text-body"
              >
                <div className="min-w-0">
                  <p className="font-medium truncate">{override.scope_name || override.scope_ref}</p>
                  <p className="text-body-sm text-fg-tertiary">
                    {SCOPE_TYPE_LABELS[override.scope_type] ?? override.scope_type}
                    {override.scan_config_name ? ` · ${override.scan_config_name}` : ''} · sigma{' '}
                    {override.sigma_threshold} · min expected {override.min_expected_count} ·{' '}
                    {countOf(override.false_positive_count, 'false positive', 'false positives')}
                  </p>
                </div>
                {canWrite && (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={removeMut.isPending}
                    onClick={() => { void handleRemove(override.id, override.scope_name || override.scope_ref) }}
                    aria-label={`Remove override for ${override.scope_name || override.scope_ref}`}
                  >
                    Remove
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}

        {removeMut.isError && (
          <p className="text-body text-destructive">{getErrorMessage(removeMut.error)}</p>
        )}
      </div>
    </Panel>
  )
}

/**
 * One setting: its label, the input, ONE line of help, and the full
 * explanation behind "Learn more". Each field used to carry three to
 * five lines of 12px text, so the page read as a wall of grey and paired
 * fields' help blocks misaligned their rows.
 */
function SettingField({
  id,
  label,
  hint,
  more,
  children,
}: {
  id: string
  label: string
  hint: ReactNode
  more?: ReactNode
  children: ReactNode
}) {
  return (
    <div className="grid content-start gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
      <p id={`${id}-hint`} className="m-0 text-caption text-fg-tertiary">{hint}</p>
      {more && (
        <details className="text-caption text-fg-tertiary">
          <summary className="w-fit cursor-pointer select-none underline-offset-2 hover:underline">
            Learn more
          </summary>
          <p className="mt-1 mb-0">{more}</p>
        </details>
      )}
    </div>
  )
}

/** A titled group of fields inside the Detection section. */
function SettingGroup({ title, lead, children }: { title: string; lead: string; children: ReactNode }) {
  const headingId = useId()
  return (
    <section aria-labelledby={headingId} className="grid gap-3">
      <div>
        <h3 id={headingId} className="m-0 text-body-sm font-semibold">{title}</h3>
        <p className="m-0 text-caption text-fg-tertiary">{lead}</p>
      </div>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">{children}</div>
    </section>
  )
}

const SCOPE_CHECKBOXES = [
  { key: 'detect_project_total', label: 'Project total' },
  { key: 'detect_event_types', label: 'Event types' },
  { key: 'detect_events', label: 'Events' },
  { key: 'detect_metrics', label: 'Metrics' },
] as const satisfies readonly { key: keyof ProjectAnomalySettings; label: string }[]

export function MonitoringTab({ slug }: { slug: string }) {
  const qc = useQueryClient()
  const canWrite = useCanWriteProject()
  const { confirm, dialog } = useConfirm()
  const settingsQuery = useQuery({
    queryKey: projectAnomalySettingsKey(slug),
    queryFn: () => anomalySettingsApi.get(slug),
    // Rendered as an ErrorState below, with a retry.
    meta: SILENT_ERROR_META,
  })
  const settings = settingsQuery.data

  const updateMut = useMutation({
    // Its error is rendered under the card.
    meta: SILENT_ERROR_META,
    mutationFn: (data: Partial<ProjectAnomalySettings>) => anomalySettingsApi.update(slug, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: projectAnomalySettingsKey(slug) })
    },
  })

  const baselineWindowId = useId()
  const minHistoryId = useId()
  const sigmaThresholdId = useId()
  const minExpectedCountId = useId()
  const recentSignalWindowId = useId()
  const settlingMinutesId = useId()

  // A NumberSetting reads the promise to reset its draft on a refusal and to say
  // "Saved" on success; the error itself is rendered once, under the card, so
  // the rejection is handled there rather than left unhandled here.
  const commit = (data: Partial<ProjectAnomalySettings>) => updateMut.mutateAsync(data)

  // Turning detection off stops every new signal and every alert in the
  // project the moment the switch moves (autosave) — further-reaching than a
  // rule delete, which is confirmed. Switching it back on needs no warning.
  const setDetectionEnabled = async (enabled: boolean) => {
    if (!enabled) {
      const ok = await confirm({
        title: 'Stop detecting anomalies in this project?',
        message: 'No new signals or alerts will be raised until detection is turned back on. Signals already raised stay on the Anomalies page.',
        confirmLabel: 'Turn off detection',
        variant: 'danger',
      })
      if (!ok) return
    }
    updateMut.mutate({ anomaly_detection_enabled: enabled })
  }

  if (settingsQuery.isError && !settings) {
    // A failed load used to read "Loading detection settings…" forever.
    return (
      <ErrorState
        title="Couldn't load detection settings"
        error={settingsQuery.error}
        onRetry={() => { void settingsQuery.refetch() }}
        retryLabel="Retry"
      />
    )
  }

  if (!settings) {
    // The page's shape, not a sentence (#237).
    return <PageSkeleton variant="settings" label="Loading detection settings…" />
  }

  // The settling allowance and the open signal window constrain each other: a
  // bucket held back past the window is already stale when it is finally scored,
  // so every signal on the project would read as closed on the Anomalies page
  // while alerts kept firing. The backend refuses that pair from both directions
  // (see settling_window_conflict); these bounds are the hint that keeps an
  // operator from walking into the 422.
  const settlingCeilingMinutes = Math.max(
    0,
    Math.min(1440, settings.recent_signal_window_hours * 60 - 1),
  )
  const windowFloorHours = Math.max(
    1,
    Math.floor(settings.anomaly_ingestion_settling_minutes / 60) + 1,
  )

  // One line for the bucket pair. Deliberately says "the series being scored"
  // and not "the scan": these settings also govern catalog metrics, and a
  // MetricDefinition carries its own interval independent of any scan config —
  // so a daily metric under an hourly scan makes 14 buckets fourteen DAYS.
  const bucketNote = (
    <>
      A bucket is one collection interval of the series being scored. On an hourly
      scan, {settings.baseline_window_buckets} buckets of baseline is{' '}
      {settings.baseline_window_buckets} hours; on a daily catalog metric it is{' '}
      {settings.baseline_window_buckets} days.
    </>
  )

  return (
    <PageContainer className="space-y-4">
      {dialog}
      {/* "Detection settings" — the same words as the buttons on the Anomalies
          and Alerting pages that lead here. The surface used to call itself
          "Monitoring" while its only card called itself "Anomaly Detection",
          giving one thing three names. Detection raises
          SIGNALS; alert rules are layered on top. The shared page header gives
          it a real h1. */}
      <PageHeader
        eyebrow="Observe"
        title="Detection settings"
        description="How tripl detects signals — spikes and drops in volume — across every scan in this project."
      />

      {!canWrite && <ReadOnlyNotice />}
      {/* Persistent while it is off, not only at the moment of the switch:
          the page otherwise looks configured and working. */}
      {!settings.anomaly_detection_enabled && (
        <p
          role="status"
          className="flex items-start gap-2 rounded-card border px-3 py-2.5 text-body-sm border-warning bg-warning-soft"
        >
          <TriangleAlert aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-warning" />
          {DETECTION_OFF_MESSAGE}
        </p>
      )}
      {settingsQuery.isError && (
        // A failed refresh after an autosave keeps the settings on screen
        // rather than replacing the whole tab.
        <p role="alert" className="text-body-sm text-destructive">
          Couldn't refresh detection settings: {getErrorMessage(settingsQuery.error)}
        </p>
      )}

      {canWrite ? (
        <Panel
          title="Detection"
          subtitle="Scans with a time column and a collection interval inherit these settings."
          right={
            <div className="flex items-center gap-3">
              <span className="min-w-16 text-right text-body-sm font-medium text-fg-tertiary">
                {settings.anomaly_detection_enabled ? 'Enabled' : 'Disabled'}
              </span>
              <Switch
                checked={settings.anomaly_detection_enabled}
                onCheckedChange={checked => { void setDetectionEnabled(checked) }}
                aria-label="Toggle signal detection"
              />
            </div>
          }
        >
          <div className="space-y-6 p-4">
            {/* Four scopes, not three: catalog metrics have always been detected
                (detect_metrics defaults to on) but had no control here, so the
                only way to stop scoring them was to disable detection entirely.

                The legend and the sentence under it are the whole reason this is
                a fieldset: four checked boxes decide WHAT GETS SCORED AT ALL.
                `min-w-0` undoes the UA default `min-inline-size: min-content` on
                fieldset, which would stop the grid inside from ever shrinking. */}
            <fieldset className="min-w-0">
              <legend className="text-body-sm font-semibold">Score these scopes</legend>
              <p className="mt-0.5 mb-3 text-caption text-fg-tertiary">
                Detection scores only the scopes checked here. Unchecking one stops new signals
                being raised for it; signals already raised stay on the Anomalies page.
              </p>
              <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                {SCOPE_CHECKBOXES.map(scope => (
                  <label key={scope.key} className="flex items-center gap-2 text-body">
                    <Checkbox
                      checked={settings[scope.key]}
                      onCheckedChange={checked => {
                        const patch: Partial<ProjectAnomalySettings> = {}
                        patch[scope.key] = !!checked
                        updateMut.mutate(patch)
                      }}
                    />
                    {scope.label}
                  </label>
                ))}
              </div>
            </fieldset>

            {/* Two groups: what counts as unusual, and when a bucket is
                judged. Sentence-case labels and `content-start` cells hold for
                every field. */}
            <SettingGroup
              title="Sensitivity"
              lead="What counts as unusual enough to raise a signal."
            >
              {/* The two settings that decide whether anything is flagged at all:
                  `_rolling_anomaly_at` skips a bucket whose
                  baseline expects fewer than min_expected_count, then flags it
                  only when |z| reaches the sigma threshold. */}
              <SettingField
                id={sigmaThresholdId}
                label="Sigma threshold"
                hint="Higher is quieter; lower catches smaller moves."
                more="How far a bucket has to sit from its baseline before it is flagged, counted in standard deviations of that baseline. Raise it for a quieter detector; lower it to catch smaller moves, at the cost of more signals."
              >
                <NumberSetting
                  id={sigmaThresholdId}
                  min={0.1}
                  step="0.1"
                  value={settings.sigma_threshold}
                  onCommit={v => commit({ sigma_threshold: v })}
                  describedBy={`${sigmaThresholdId}-hint`}
                />
              </SettingField>
              <SettingField
                id={minExpectedCountId}
                label="Min expected count"
                hint="Skip scopes too quiet to judge. 0 scores every bucket."
                more={
                  <>
                    A floor on the baseline, not on the bucket: any bucket whose baseline expects
                    fewer than {settings.min_expected_count} is skipped, so a quiet series cannot
                    raise a spike off a handful of events. 0 scores every bucket.
                  </>
                }
              >
                <NumberSetting
                  id={minExpectedCountId}
                  min={0}
                  value={settings.min_expected_count}
                  onCommit={v => commit({ min_expected_count: v })}
                  describedBy={`${minExpectedCountId}-hint`}
                />
              </SettingField>
            </SettingGroup>

            <SettingGroup
              title="Timing"
              lead="How much history a bucket is judged against, and when."
            >
              {/* Both counted in BUCKETS, and the labels say so: a bare "14" on
                  an hourly scan reads as fourteen days when it means fourteen
                  hours. */}
              <SettingField
                id={baselineWindowId}
                label="Baseline window (buckets)"
                hint="How many past buckets make the baseline."
                more={bucketNote}
              >
                <NumberSetting
                  id={baselineWindowId}
                  min={1}
                  value={settings.baseline_window_buckets}
                  onCommit={v => commit({ baseline_window_buckets: v })}
                  describedBy={`${baselineWindowId}-hint`}
                />
              </SettingField>
              <SettingField
                id={minHistoryId}
                label="Min history (buckets)"
                hint="Buckets a series needs before it is scored."
              >
                <NumberSetting
                  id={minHistoryId}
                  min={1}
                  value={settings.min_history_buckets}
                  onCommit={v => commit({ min_history_buckets: v })}
                  describedBy={`${minHistoryId}-hint`}
                />
              </SettingField>
              <SettingField
                id={recentSignalWindowId}
                label="Open signal window (hours)"
                hint={`How long a signal stays open. ${windowFloorHours}–720 hours.`}
                more={
                  <>
                    How long an anomaly keeps counting as an open signal on the Anomalies page
                    and in the sidebar badge. Alert delivery is unaffected. The floor is the
                    settling allowance beside it ({settings.anomaly_ingestion_settling_minutes} min):
                    a window that does not outlast the allowance would close every signal before
                    it could be scored.
                  </>
                }
              >
                <NumberSetting
                  id={recentSignalWindowId}
                  min={windowFloorHours}
                  max={720}
                  value={settings.recent_signal_window_hours}
                  onCommit={v => commit({ recent_signal_window_hours: v })}
                  describedBy={`${recentSignalWindowId}-hint`}
                />
              </SettingField>
              <SettingField
                id={settlingMinutesId}
                label="Ingestion settling (minutes)"
                hint={`Wait for late rows before scoring. 0–${settlingCeilingMinutes} minutes.`}
                more={
                  <>
                    How long a warehouse keeps delivering rows for a bucket after that bucket
                    closes. Those buckets are still collected and charted but raise no signal
                    until the allowance passes, so a half-delivered bucket is not read as a
                    drop — and the allowance is the detection latency you pay for that. The
                    ceiling is one minute under the open signal window beside it{' '}
                    ({settings.recent_signal_window_hours}h), and never above 1440 (24 hours).
                  </>
                }
              >
                <NumberSetting
                  id={settlingMinutesId}
                  min={0}
                  max={settlingCeilingMinutes}
                  value={settings.anomaly_ingestion_settling_minutes}
                  onCommit={v => commit({ anomaly_ingestion_settling_minutes: v })}
                  describedBy={`${settlingMinutesId}-hint`}
                />
              </SettingField>
            </SettingGroup>

            <p className="text-caption text-fg-tertiary">
              Changes apply from the next metrics collection. Chart markers appear only when a
              scope&apos;s latest bucket is anomalous.
            </p>

            {updateMut.isError && (
              <p className="text-body text-destructive">{getErrorMessage(updateMut.error)}</p>
            )}
          </div>
        </Panel>
      ) : (
        // A viewer reads the settings; a disabled form kept live borders and
        // editing hints on controls that did nothing (#237 rule 4).
        <Panel title="Detection" subtitle="Scans with a time column and a collection interval inherit these settings.">
          <div className="p-4">
            <ReadOnlyDefinition
              items={[
                { label: 'Detection', value: settings.anomaly_detection_enabled ? 'Enabled' : 'Disabled' },
                {
                  label: 'Scored scopes',
                  value: SCOPE_CHECKBOXES.filter(scope => settings[scope.key]).map(scope => scope.label).join(', ') || 'None',
                },
                { label: 'Sigma threshold', value: String(settings.sigma_threshold) },
                { label: 'Min expected count', value: String(settings.min_expected_count) },
                { label: 'Baseline window', value: `${settings.baseline_window_buckets} buckets` },
                { label: 'Min history', value: `${settings.min_history_buckets} buckets` },
                { label: 'Open signal window', value: `${settings.recent_signal_window_hours} hours` },
                { label: 'Ingestion settling', value: `${settings.anomaly_ingestion_settling_minutes} minutes` },
              ]}
            />
          </div>
        </Panel>
      )}

      <HolidayCalendarPanel slug={slug} country={settings.holiday_country ?? null} canWrite={canWrite} />

      <ScopeOverridesCard slug={slug} canWrite={canWrite} />
    </PageContainer>
  )
}
