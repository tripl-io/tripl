import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useMutation } from '@tanstack/react-query'
import { AlertTriangle, History } from 'lucide-react'

import { alertingApi, type AlertRuleUpdatePayload } from '@/api/alerting'
import { useDemoScenarioActions } from '@/demo/demoScenarioContext'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Chip } from '@/components/primitives/chip'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import type { AlertMessageFormat, AlertRule, AlertRuleSimulateResponse, ScanConfig } from '@/types'
import { getErrorMessage } from '@/lib/utils'
import { formatIncidentCount, scopeHasDirection, scopeKindLabel } from '@/lib/alertStatus'
import { APP_LOCALE } from '@/lib/format'
import { formatPercentDelta } from '@/lib/percentDelta'
import { formatCooldown } from './constants'

const DAYS_OPTIONS = [1, 3, 7, 14, 30] as const

/**
 * The preview caption used to pick its wording from
 * `firings[0]?.scope_type ? 'destination' : 'Slack/Telegram'` — a field that
 * says nothing whatsoever about how a message is rendered, so the line was a
 * coin flip dressed as information. What the preview is actually
 * rendered with is the rule's own message format, so that is what it now names.
 */
const MESSAGE_FORMAT_LABEL: Record<AlertMessageFormat, string> = {
  plain: 'plain text',
  slack_mrkdwn: 'Slack mrkdwn',
  telegram_html: 'Telegram HTML',
  telegram_markdownv2: 'Telegram MarkdownV2',
}

// The bounds the simulate route enforces on sigma (`gt=0, le=RATCHET_SIGMA_CAP`
// in api/v1/alerting.py). They are mirrored here rather than left to the server
// because the failure mode is silent: the input allowed 0, `parseOverride`
// happily returned it, and every replay then 422'd with nothing on screen
// explaining why. A bound the UI does not know is a bound the user discovers by
// failing.
const SIGMA_MIN_EXCLUSIVE = 0
const SIGMA_MAX = 10
// The same treatment for the cooldown override, which the route declares as
// `int, ge=0, le=10080` (a week). `parseOverride` accepted 1.5 or 20000 and the
// replay came back as "Replay failed: cooldown_minutes_override: Input should be
// a valid integer…".
const COOLDOWN_OVERRIDE_MAX = 10080

/** Thresholds to try WITHOUT saving them to the rule. */
interface ReplayOverrides {
  cooldownMinutes?: number
  minPercentDelta?: number
  minExpectedCount?: number
  sigmaThreshold?: number
}

/** Everything a run was asked for — what a result is only valid for. */
interface ReplayRequest {
  n: number
  overrides: ReplayOverrides | null
}

type ReplayResult = {
  saved: AlertRuleSimulateResponse
  override: AlertRuleSimulateResponse | null
  /** The request that produced it, to tell when the inputs have moved on. */
  request: ReplayRequest
}

function sameRequest(a: ReplayRequest, b: ReplayRequest): boolean {
  return a.n === b.n && JSON.stringify(a.overrides ?? {}) === JSON.stringify(b.overrides ?? {})
}

/**
 * A blank box means "leave this one alone", which is a different instruction
 * from 0 — `Number('')` is 0, so parsing without this guard would silently
 * replay every empty field as the most permissive threshold there is.
 *
 * Text that is there but is not a usable value (negative, not finite) comes
 * back as NaN, NOT as blank: reading "-5" as "no override" ran the replay on
 * the saved value while the box still showed -5. Every caller treats
 * NaN as invalid.
 */
function parseOverride(text: string): number | null {
  const trimmed = text.trim()
  if (trimmed === '') return null
  const value = Number(trimmed)
  if (!Number.isFinite(value) || value < 0) return Number.NaN
  return value
}

/** Present but unusable: the box gets an inline error and Replay is blocked. */
function isInvalidOverride(value: number | null): boolean {
  return value !== null && Number.isNaN(value)
}

/**
 * "Sep 24, 00:00" — short enough to sit in a 7rem column without running into
 * Scope, in sans with tabular figures. The year says nothing inside a
 * replay window of at most 30 days.
 */
function formatReplayWhen(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString(APP_LOCALE, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  })
}

/** One override input: a real label, and the value it replaces as helper text. */
function OverrideField({
  id,
  label,
  helper,
  error,
  children,
}: {
  id: string
  label: string
  helper: string
  error: string | null
  children: ReactNode
}) {
  return (
    <div className="grid content-start gap-1">
      <Label htmlFor={id} className="text-body-sm">{label}</Label>
      {children}
      <p id={`${id}-helper`} className="m-0 text-caption text-fg-subtle">{helper}</p>
      {error && (
        <p role="alert" className="m-0 max-w-48 text-caption text-destructive">{error}</p>
      )}
    </div>
  )
}

function ThresholdRow({
  label,
  used,
  saved,
}: {
  label: string
  used: string
  saved: string
}) {
  const changed = used !== saved
  return (
    <div className="min-w-0">
      <dt className="micro-label text-fg-tertiary">{label}</dt>
      <dd className={changed ? 'text-body-sm font-medium text-foreground' : 'text-body-sm text-fg-tertiary'}>
        {used}
        {changed && <span className="text-fg-tertiary"> (saved {saved})</span>}
      </dd>
    </div>
  )
}

function FiringsCountBadge({
  label,
  count,
  noisy,
}: {
  label: string
  count: number
  noisy: boolean
}) {
  return (
    <div className="flex flex-col items-start rounded-md border bg-muted/30 px-3 py-2">
      <div className="micro-label text-fg-tertiary">{label}</div>
      <div className="flex items-center gap-2">
        <span className="text-heading font-semibold tnum">{count}</span>
        <span className="text-body-sm text-fg-tertiary">{count === 1 ? 'firing' : 'firings'}</span>
        {noisy && (
          <Chip tone="danger" icon={<AlertTriangle aria-hidden="true" />}>
            Noisy
          </Chip>
        )}
      </div>
    </div>
  )
}

export function RuleReplayDialog({
  open,
  onOpenChange,
  slug,
  destinationId,
  rule,
  scans,
  draft = null,
}: {
  open: boolean
  onOpenChange: (value: boolean) => void
  slug: string
  destinationId: string
  rule: AlertRule
  scans: Pick<ScanConfig, 'id' | 'name'>[]
  /**
   * The editor's unsaved edits (the PATCH body Save would send). Given, every
   * run replays the saved rule with these laid over it, server-side and
   * without writing them; the overrides below then vary the EDITED
   * thresholds.
   */
  draft?: AlertRuleUpdatePayload | null
}) {
  const [days, setDays] = useState<number>(7)
  const [cooldownText, setCooldownText] = useState<string>('')
  const [minPercentText, setMinPercentText] = useState<string>('')
  const [minExpectedText, setMinExpectedText] = useState<string>('')
  const [sigmaText, setSigmaText] = useState<string>('')
  const [result, setResult] = useState<ReplayResult | null>(null)
  const { notifyStepCompleted } = useDemoScenarioActions()
  // What an override is compared with: the edited value when replaying a
  // draft, else the saved one.
  const base = {
    cooldown_minutes: draft?.cooldown_minutes ?? rule.cooldown_minutes,
    min_percent_delta: draft?.min_percent_delta ?? rule.min_percent_delta,
    min_expected_count: draft?.min_expected_count ?? rule.min_expected_count,
    message_format: draft?.message_format ?? rule.message_format,
  }
  const baseLabel = draft ? 'edited' : 'saved'
  const baseLabelTitle = draft ? 'Edited' : 'Saved'

  // Only a value that actually DIFFERS from the rule counts as an override:
  // typing the saved number back in would otherwise cost a second identical
  // request and label the result "Override" when nothing was overridden.
  const requestedOverrides: ReplayOverrides = {}
  const cooldown = parseOverride(cooldownText)
  const cooldownInvalid =
    cooldown !== null && (!Number.isInteger(cooldown) || cooldown > COOLDOWN_OVERRIDE_MAX)
  if (cooldown !== null && !cooldownInvalid && cooldown !== base.cooldown_minutes) {
    requestedOverrides.cooldownMinutes = cooldown
  }
  const minPercent = parseOverride(minPercentText)
  const minPercentInvalid = isInvalidOverride(minPercent)
  if (minPercent !== null && !minPercentInvalid && minPercent !== base.min_percent_delta) {
    requestedOverrides.minPercentDelta = minPercent
  }
  const minExpected = parseOverride(minExpectedText)
  const minExpectedInvalid = isInvalidOverride(minExpected)
  if (minExpected !== null && !minExpectedInvalid && minExpected !== base.min_expected_count) {
    requestedOverrides.minExpectedCount = minExpected
  }
  // Sigma has no rule-level column to compare against — the detector's own
  // default is the baseline — so anything typed here is a change by definition.
  const sigma = parseOverride(sigmaText)
  const sigmaOutOfRange =
    sigma !== null
    && (isInvalidOverride(sigma) || sigma <= SIGMA_MIN_EXCLUSIVE || sigma > SIGMA_MAX)
  if (sigma !== null && !sigmaOutOfRange) {
    requestedOverrides.sigmaThreshold = sigma
  }
  const hasOverrides = Object.keys(requestedOverrides).length > 0
  const currentRequest: ReplayRequest = {
    n: days,
    overrides: hasOverrides ? requestedOverrides : null,
  }

  const simulateMut = useMutation({
    mutationFn: async (request: ReplayRequest): Promise<ReplayResult> => {
      const { n, overrides } = request
      // Two runs, always: `*_used`/`*_saved` name the thresholds a single run
      // applied, but only a second run over the SAME window says how many
      // firings the change would actually have removed.
      // A draft rides every run, so the comparison is edits vs edits+overrides.
      const run = (runOverrides?: ReplayOverrides) =>
        draft
          ? alertingApi.simulateRule(slug, destinationId, rule.id, n, runOverrides, draft)
          : runOverrides
            ? alertingApi.simulateRule(slug, destinationId, rule.id, n, runOverrides)
            : alertingApi.simulateRule(slug, destinationId, rule.id, n)
      const savedPromise = run()
      if (!overrides) {
        const saved = await savedPromise
        return { saved, override: null, request }
      }
      const [saved, overrideResp] = await Promise.all([
        savedPromise,
        run(overrides),
      ])
      return { saved, override: overrideResp, request }
    },
    onSuccess: (data) => {
      setResult(data)
      // A finished replay lands the alerting chapter's simulate step — inert
      // outside the demo scenario (the reducer drops every other step).
      notifyStepCompleted('alerting/simulate')
    },
  })

  // Run the rule as it stands the moment the dialog opens: the question
  // it exists for — "what would this have sent last week?" — needed a second
  // click, and the dialog opened on a row of inputs with nothing to look at.
  // Once per opening; the inputs then re-run it on demand.
  const autoRan = useRef(false)
  const { mutate: runReplay } = simulateMut
  useEffect(() => {
    if (!open) {
      autoRan.current = false
      return
    }
    if (autoRan.current) return
    autoRan.current = true
    runReplay({ n: days, overrides: null })
    // Only the opening triggers it; later edits wait for the Replay button.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const handleOpenChange = (value: boolean) => {
    onOpenChange(value)
    if (!value) {
      setResult(null)
      simulateMut.reset()
      setCooldownText('')
      setMinPercentText('')
      setMinExpectedText('')
      setSigmaText('')
    }
  }

  const displayResult = result?.override ?? result?.saved ?? null
  // A result answers the window and overrides it was run with. Changing either
  // afterwards used to leave "Considered N", the firings and the preview on
  // screen as if they answered the new inputs. They stay — a
  // comparison is often the point — but dimmed, and say so.
  const resultIsStale = result !== null && !sameRequest(result.request, currentRequest)

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      {/* overflow-hidden: DialogBody owns the scroll, so wide replay data
          scrolls inside its own region and never widens or side-scrolls the
          dialog itself. */}
      <DialogContent className="max-h-[calc(100vh-2rem)] w-[calc(100vw-2rem)] max-w-4xl min-w-0 overflow-hidden">
        {/* Left-aligned at every width, clear of the close button:
            the centred two-line title ran into ✕ on a phone. */}
        <DialogHeader className="pr-10 text-left">
          <DialogTitle className="flex items-start gap-2">
            <History aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
            <span className="min-w-0 break-words">
              Replay rule “{rule.name}”{draft ? ' with your unsaved edits' : ''}
            </span>
          </DialogTitle>
          <p className="m-0 text-body-sm text-fg-subtle">
            See what this rule would have sent in the chosen window. Nothing is sent or saved.
          </p>
        </DialogHeader>

        <DialogBody className="min-w-0 space-y-3">
          <div className="flex flex-wrap items-start gap-3">
            <div className="grid content-start gap-1">
              <Label htmlFor="replay-window" className="text-body-sm">Window</Label>
              <Select value={String(days)} onValueChange={(v) => setDays(Number(v))}>
                <SelectTrigger id="replay-window" className="w-32">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {DAYS_OPTIONS.map((option) => (
                    <SelectItem key={option} value={String(option)}>
                      Last {option}d
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {/* Every threshold the simulation can vary, tried WITHOUT saving it:
                answering "would Min % 300 cut these incidents" used to mean
                writing 300 onto the rule that is live-routing to a real channel
                and waiting to find out. */}
            <OverrideField
              id="replay-cooldown"
              label="Cooldown (minutes)"
              helper={`${baseLabelTitle}: ${formatCooldown(base.cooldown_minutes)}`}
              error={cooldownInvalid
                ? `Whole minutes from 0 to ${COOLDOWN_OVERRIDE_MAX}, or blank for the ${baseLabel} value.`
                : null}
            >
              <Input
                id="replay-cooldown"
                type="number"
                min={0}
                max={COOLDOWN_OVERRIDE_MAX}
                step={1}
                aria-invalid={cooldownInvalid || undefined}
                aria-describedby="replay-cooldown-helper"
                value={cooldownText}
                onChange={(e) => setCooldownText(e.target.value)}
                className="w-32"
              />
            </OverrideField>
            <OverrideField
              id="replay-min-pct"
              label="Min change (%)"
              helper={`${baseLabelTitle}: ${base.min_percent_delta}%`}
              error={minPercentInvalid ? `0 or more, or blank for the ${baseLabel} value.` : null}
            >
              <Input
                id="replay-min-pct"
                type="number"
                min={0}
                step="0.1"
                aria-invalid={minPercentInvalid || undefined}
                aria-describedby="replay-min-pct-helper"
                value={minPercentText}
                onChange={(e) => setMinPercentText(e.target.value)}
                className="w-28"
              />
            </OverrideField>
            <OverrideField
              id="replay-min-expected"
              label="Min expected count"
              helper={`${baseLabelTitle}: ${base.min_expected_count}`}
              error={minExpectedInvalid ? `0 or more, or blank for the ${baseLabel} value.` : null}
            >
              <Input
                id="replay-min-expected"
                type="number"
                min={0}
                step="0.1"
                aria-invalid={minExpectedInvalid || undefined}
                aria-describedby="replay-min-expected-helper"
                value={minExpectedText}
                onChange={(e) => setMinExpectedText(e.target.value)}
                className="w-28"
              />
            </OverrideField>
            <OverrideField
              id="replay-sigma"
              label="Sigma threshold"
              helper="Blank: the project's detection setting"
              error={sigmaOutOfRange
                ? `Between ${SIGMA_MIN_EXCLUSIVE} and ${SIGMA_MAX}, or blank for the detection setting.`
                : null}
            >
              <Input
                id="replay-sigma"
                type="number"
                min={0.1}
                max={SIGMA_MAX}
                step="0.1"
                aria-invalid={sigmaOutOfRange || undefined}
                aria-describedby="replay-sigma-helper"
                value={sigmaText}
                onChange={(e) => setSigmaText(e.target.value)}
                className="w-28"
              />
            </OverrideField>
            <Button
              className="mt-5"
              onClick={() => simulateMut.mutate(currentRequest)}
              // Blocked on any invalid override rather than sent
              // and 422'd: the replay would fail for a reason the dialog never
              // showed.
              disabled={
                simulateMut.isPending
                || sigmaOutOfRange
                || cooldownInvalid
                || minPercentInvalid
                || minExpectedInvalid
              }
            >
              {simulateMut.isPending ? 'Replaying…' : 'Replay'}
            </Button>
            {displayResult && (
              <div className={`ml-auto text-right text-body-sm text-fg-tertiary ${resultIsStale ? 'opacity-50' : ''}`}>
                <div>
                  Considered{' '}
                  <span className="font-medium text-foreground">
                    {displayResult.anomalies_considered}
                  </span>{' '}
                  anomalies
                </div>
                <div>
                  Matched{' '}
                  <span className="font-medium text-foreground">
                    {displayResult.matched_before_cooldown}
                  </span>{' '}
                  before cooldown
                </div>
              </div>
            )}
          </div>

          {simulateMut.isError && (
            <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-body text-destructive">
              Replay failed: {getErrorMessage(simulateMut.error)}
            </div>
          )}

          {resultIsStale && (
            <p role="status" className="rounded-md border border-dashed p-2 text-body-sm text-fg-tertiary">
              Settings changed since this replay — press Replay to see results for them.
            </p>
          )}

          {result && (
            <div className={resultIsStale ? 'space-y-3 opacity-50' : 'space-y-3'}>
              <div className="flex flex-wrap items-center gap-3 text-body">
                <FiringsCountBadge
                  label={draft ? 'Your edits' : 'Saved thresholds'}
                  count={result.saved.firings.length}
                  noisy={result.saved.noisy}
                />
                {result.override && (
                  <>
                    <span className="text-fg-tertiary">→</span>
                    <FiringsCountBadge
                      label="With your overrides"
                      count={result.override.firings.length}
                      noisy={result.override.noisy}
                    />
                  </>
                )}
              </div>

              {/* What the shown run actually applied, against what the rule
                  stores. Without both halves a preview run and the rule's real
                  behaviour are indistinguishable on screen. */}
              {displayResult && (
                <div className="space-y-1">
                  <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4">
                    <ThresholdRow
                      label="Cooldown"
                      used={formatCooldown(displayResult.cooldown_minutes_used)}
                      saved={formatCooldown(displayResult.cooldown_minutes_saved)}
                    />
                    <ThresholdRow
                      label="Min %"
                      used={String(displayResult.min_percent_delta_used)}
                      saved={String(displayResult.min_percent_delta_saved)}
                    />
                    <ThresholdRow
                      label="Min expected"
                      used={String(displayResult.min_expected_count_used)}
                      saved={String(displayResult.min_expected_count_saved)}
                    />
                    {/* Sigma has no rule-level column: `saved` is the project's
                        Detection-settings threshold, the one the detector scores
                        with. The null arms below are defensive
                        — the contract is still `number | null` and older cached
                        responses can hold one — but this backend always sends a
                        number, so "detector default" is no longer a state a
                        replay of a live project reaches. */}
                    <ThresholdRow
                      label="Sigma"
                      used={displayResult.sigma_threshold_used === null
                        ? 'detector default'
                        : String(displayResult.sigma_threshold_used)}
                      saved={displayResult.sigma_threshold_saved === null
                        ? 'detector default'
                        : String(displayResult.sigma_threshold_saved)}
                    />
                  </dl>
                  <p className="text-caption text-fg-tertiary">
                    Nothing here is saved to the rule — it keeps routing on its stored thresholds.
                  </p>
                </div>
              )}

              {displayResult && displayResult.firings.length === 0 ? (
                <div className="rounded-md border border-dashed p-4 text-center text-body text-fg-tertiary">
                  No firings in this window. Try widening the range or relaxing thresholds.
                </div>
              ) : (
                displayResult && (
                  // A focusable named scroll region lets keyboard users reach columns
                  // that sit beyond a narrow viewport.
                  // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- Intentional scroll target.
                  <div role="region" aria-label="Replay firings" tabIndex={0}
                    className="max-h-72 min-w-0 max-w-full overflow-x-auto overflow-y-auto rounded-md border"
                  >
                    {/* This region stays the one scroll container (and the
                        focusable one), so the table's own wrapper does not clip.
                        Scan and Expected are the first to go on a phone: the
                        scope names the series and Δ% carries the comparison. */}
                    <Table
                      scroll={false}
                      className="min-w-[560px] table-fixed text-left text-body-sm md:min-w-[840px]"
                    >
                      <TableHeader className="bg-muted/50">
                        <TableRow className="hover:bg-transparent">
                          <TableHead scope="col" className="h-auto w-28 py-2">When</TableHead>
                          <TableHead scope="col" className="h-auto w-64 py-2">Scope</TableHead>
                          <TableHead scope="col" className="hidden h-auto w-32 py-2 md:table-cell">Scan</TableHead>
                          <TableHead scope="col" className="h-auto w-20 py-2">Dir</TableHead>
                          <TableHead scope="col" className="h-auto w-20 py-2 text-right">Actual</TableHead>
                          <TableHead scope="col" className="hidden h-auto w-24 py-2 text-right md:table-cell">Expected</TableHead>
                          <TableHead scope="col" className="h-auto w-20 py-2 pr-3 text-right">Δ%</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {displayResult.firings.map((firing) => (
                          <TableRow key={firing.anomaly_id}>
                            <TableCell className="whitespace-nowrap py-1.5 tnum">{formatReplayWhen(firing.bucket)}</TableCell>
                            {/* The kind through the shared `scopeKindLabel`, the
                                same words the Inbox chips use, rather than the
                                raw enum. The column shipped printing
                                `firing.scope_type`, which was survivable while
                                the replay could only produce `project_total` /
                                `event_type` / `event`; since it also replays
                                value drifts and release regressions
                                 that cell would otherwise read
                                `variable_value_drift`, in a table whose
                                neighbouring surfaces call it "value drift".

                                `title` prefers `rendered_item` — the exact line
                                this firing would have been sent as, built by
                                the backend's one shared item renderer — so the
                                per-scope sentence a narrow cell cannot hold is
                                a hover away and is never a second copy of the
                                wording. */}
                            <TableCell
                              className="truncate py-1.5"
                              title={firing.rendered_item ?? firing.scope_name}
                            >
                              <span className="text-fg-tertiary">
                                {scopeKindLabel(firing.scope_type)}
                              </span>{' '}
                              {firing.scope_name}
                              {firing.drift_field && (
                                <div className="truncate text-caption text-fg-tertiary">
                                  {firing.drift_type}: {firing.drift_field}
                                </div>
                              )}
                            </TableCell>
                            <TableCell
                              className="hidden truncate py-1.5 md:table-cell"
                              title={firing.scan_config_id ?? 'Project-wide'}
                            >
                              {firing.scan_config_id === null
                                ? 'Project-wide'
                                : (scans.find(scan => scan.id === firing.scan_config_id)?.name
                                  ?? `Scan ${firing.scan_config_id.slice(0, 8)}`)}
                            </TableCell>
                            {/* A drift has no direction and no relative change:
                                its row says "spike" only so the rule's Spikes
                                toggle gates it, and its Δ% compares row counts.
                                What drifted is under the scope name, and the
                                scope cell's hover holds the whole sent line. */}
                            <TableCell className="py-1.5">
                              {scopeHasDirection(firing.scope_type) ? (
                                <Chip variant="outline" size="xs">{firing.direction}</Chip>
                              ) : (
                                <span className="text-fg-tertiary" title="A drift has no direction">—</span>
                              )}
                            </TableCell>
                            {/* Both columns through the same formatter: rounding
                                only the baseline would leave "5780" beside
                                "3,529" in one row, and a `%` catalog metric
                                stores a fraction, so Math.round prints its
                                whole replay as "0 vs 0". */}
                            <TableCell className="py-1.5 text-right tnum">
                              {formatIncidentCount(firing.actual_count)}
                            </TableCell>
                            <TableCell className="hidden py-1.5 text-right tnum text-fg-tertiary md:table-cell">
                              {formatIncidentCount(firing.expected_count)}
                            </TableCell>
                            <TableCell className="whitespace-nowrap py-1.5 pr-3 text-right tnum">
                              {scopeHasDirection(firing.scope_type)
                                ? formatPercentDelta(firing.percent_delta, firing.expected_count)
                                : <span className="text-fg-tertiary" title="A drift has no relative change">—</span>}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                )
              )}

              {displayResult?.rendered_message && (
                <div className="min-w-0 space-y-1">
                  <div className="text-caption uppercase tracking-wide text-fg-tertiary">
                    Preview — the message this run would have sent, as{' '}
                    {MESSAGE_FORMAT_LABEL[base.message_format]}
                  </div>
                  <pre className="max-h-48 min-w-0 max-w-full overflow-auto whitespace-pre-wrap rounded-md border bg-muted/30 px-3 py-2 font-mono text-caption [overflow-wrap:anywhere]">
                    {displayResult.rendered_message}
                  </pre>
                </div>
              )}
            </div>
          )}
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={() => handleOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
