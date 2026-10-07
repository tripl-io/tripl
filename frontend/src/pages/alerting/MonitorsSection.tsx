import { formatNumber } from '@/lib/format'
import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bell, BellOff, ChevronDown, ChevronRight, History, MoreHorizontal, Pencil, Plus, Trash2, TriangleAlert } from 'lucide-react'
import { Link, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'

import { alertingApi, type AlertRuleUpdatePayload } from '@/api/alerting'
import { anomalySettingsApi } from '@/api/anomalySettings'
import { Button } from '@/components/ui/button'
import { IconButton } from '@/components/ui/icon-button'
import { Switch } from '@/components/ui/switch'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { ReadOnlyNotice, StatValueSkeleton } from '@/components/states'
import { EmptyState } from '@/components/empty-state'
import { Panel } from '@/components/settings/kit'
import { Chip } from '@/components/primitives/chip'
import { Dot } from '@/components/primitives/dot'
import { MiniStat, MiniStatStrip } from '@/components/primitives/mini-stat'
import { ScenarioCoachMark } from '@/demo/ScenarioCoachMark'
import { useDemoScenarioActions } from '@/demo/demoScenarioContext'
import { SCENARIO_SEEDED } from '@/demo/scenarioModel'
import { useConfirm } from '@/hooks/useConfirm'
import { SILENT_ERROR_META, surfaceError } from '@/lib/errorFeedback'
import { stripValueErrorPrefix } from '@/lib/alertStatus'
import { formatDateTime, formatRelativeTime } from '@/lib/datetime'
import { countOf } from '@/lib/plural'
import { MUTE_PRESETS, muteChoiceName, muteUntilIso, unmuteName } from '@/lib/mutePresets'
import { VIEWER_READ_ONLY_NOTICE } from '@/lib/permissions'
import {
  MONITOR_STATUS_LABEL as STATUS_LABEL,
  MONITOR_STATUS_TONE as STATUS_TONE,
} from '@/lib/statusLexicon'
import { useAdaptiveRefetchInterval } from '@/realtime/streamContext'
import type {
  AlertDestination,
  AlertRule,
  EventType,
  MonitorStatus,
  MonitorSummaryItem,
  ScanConfig,
} from '@/types'

import { invalidateAlertingConfig } from './alertingCache'
import { ChannelGlyph, channelLabel } from './channelMeta'
import {
  DETECTION_OFF_MESSAGE,
  defaultRuleForm,
  directionSummary,
  formatCooldown,
  isDefaultMessageTemplate,
  messageFormatForDestination,
  ruleConditionSummary,
  makeFilterUid,
  ruleFormToPayload,
  ruleToForm,
  scopeSummary,
  withMessageFormat,
  type RuleFormState,
} from './constants'
import { describeDeletionImpact } from './deletionImpact'
import { RuleEditorDialog } from './RuleEditorDialog'
import { RuleReplayDialog } from './RuleReplayDialog'
import { monitorsSummaryKey, projectAnomalySettingsKey } from '@/lib/queryKeys'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

/** A rule carrying the destination it hangs off, as the page flattens it. */
export interface RuleWithDestination extends AlertRule {
  destination_id: string
  destination_name: string
}

// One DOM for every width. The rule table used to be a fixed 840px grid
// inside a horizontal scroller, so at 768px the delete and replay icons were
// cut mid-glyph and at 390px the firing state, the switch and every action
// sat off-screen with nothing saying the table scrolled. Now:
//  - below `md` each row is a stacked card: name and state, the condition,
//    where it routes, then the switch, Edit and the "…" menu;
//  - from `md` it is the table, with "Routes to" folded into the rule cell's
//    second line until `lg` has room for its own column.
//
// The header row and each rule row are separate grids that only look like one
// table, so they must resolve the same tracks: that is why every track is
// fixed or `minmax(0, …)`, and why the action track carries a floor — the
// switch, Edit and the menu (36 + 32 + 32px and two 6px gutters) — rather
// than `auto`, which is measured per grid. Mute's durations no longer open
// inline: they live in the menu, so no row can grow wider than its
// neighbours.
//
// Every column string is written out in full: Tailwind scans source for literal
// class names, so an interpolated `grid-cols-[…]` would never be built.
const RULE_GRID_BASE = 'md:grid md:items-center md:gap-3 px-4'
const RULE_GRID_COLS
  = 'md:grid-cols-[minmax(0,2fr)_minmax(0,1.6fr)_64px_84px_minmax(112px,auto)] lg:grid-cols-[minmax(0,2fr)_minmax(0,1.6fr)_minmax(0,1fr)_64px_84px_minmax(112px,auto)]'
const RULE_GRID_COLS_READ_ONLY
  = 'md:grid-cols-[minmax(0,2fr)_minmax(0,1.6fr)_64px_84px_minmax(72px,auto)] lg:grid-cols-[minmax(0,2fr)_minmax(0,1.6fr)_minmax(0,1fr)_64px_84px_minmax(72px,auto)]'

/** The one grid definition the header row and every rule row must share. */
function ruleGridClass(canWrite: boolean): string {
  return `${RULE_GRID_BASE} ${canWrite ? RULE_GRID_COLS : RULE_GRID_COLS_READ_ONLY}`
}

/** How long a just-created rule stays highlighted in the list. */
const NEW_RULE_HIGHLIGHT_MS = 1500

/** The stat tiles that filter the rules table, by the state each one counts. */
const STATE_FILTER_LABEL: Record<MonitorStatus, string> = {
  firing: 'Firing',
  warning: 'Warning',
  healthy: 'Healthy',
}

interface MonitorsSectionProps {
  slug: string
  destinations: AlertDestination[]
  rules: RuleWithDestination[]
  eventTypes: EventType[]
  scans: ScanConfig[]
  /**
   * Whether `scans` has answered. Until it has, a scan-bound rule's scan is
   * UNKNOWN rather than missing, and must not read "unknown scan" — the words
   * for a scan that was deleted. Optional: absent means loaded.
   */
  scansLoaded?: boolean
  /** The scan list failed to load: a bound scan's name is unavailable, not pending. */
  scansFailed?: boolean
  canWrite: boolean
  /**
   * Guided setup's step 3: open the rule form prefilled for the
   * destination the reader has just created. It used to be handed to that
   * destination's card; rules no longer live there, so the section takes it.
   */
  autoOpenRuleForDestinationId: string | null
  /**
   * That destination's name, from the create response: the list may not have
   * refetched it yet when the form opens, and the form is named after it.
   */
  autoOpenRuleDestinationName?: string | null
  onAutoOpenRuleConsumed: () => void
  /** Send the reader to the Destinations section — nothing can route without one. */
  onGoToDestinations: () => void
}

/**
 * "Monitors": every alert rule in the project, with the live state that used to
 * be the standalone Monitors page's only reason to exist.
 *
 * The two surfaces described the same object. `get_monitors_summary` selects
 * AlertRule joined to AlertDestination — the same rows the destination cards
 * rendered — so a rule was read on one nav item and edited on another, which is
 * how the two drifted about its mute state. Merged here:
 * the rule list, its state, and every control that acts on a rule
 * are one screen.
 *
 * State comes from `monitors-summary` and configuration from `destination.rules`,
 * because only the former knows about AlertRuleState. A rule still renders while
 * the summary is in flight — it is not less real for its status being unknown,
 * and blanking the list on every refetch would empty it mid-incident.
 */
export function MonitorsSection({
  slug,
  destinations,
  rules,
  eventTypes,
  scans,
  scansLoaded = true,
  scansFailed = false,
  canWrite,
  autoOpenRuleForDestinationId,
  autoOpenRuleDestinationName = null,
  onAutoOpenRuleConsumed,
  onGoToDestinations,
}: MonitorsSectionProps) {
  const qc = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const { confirm, dialog } = useConfirm()
  const { notifyStepCompleted } = useDemoScenarioActions()
  const refetchInterval = useAdaptiveRefetchInterval({ activeMs: 60_000 })

  const [ruleDialogOpen, setRuleDialogOpen] = useState(false)
  const [editingRule, setEditingRule] = useState<RuleWithDestination | null>(null)
  const [replayingRule, setReplayingRule] = useState<RuleWithDestination | null>(null)
  // The editor's unsaved edits when the replay was opened from its "Replay
  // with these edits"; null replays the saved rule.
  const [replayDraft, setReplayDraft] = useState<AlertRuleUpdatePayload | null>(null)
  const [ruleForm, setRuleForm] = useState<RuleFormState>(defaultRuleForm())
  const [formDestinationId, setFormDestinationId] = useState('')
  // One rule's settings open at a time. The list is for scanning state; the
  // settings behind this are what the destination card used to print in full on
  // every rule, which is why five rules filled a screen before you could see
  // which of them was firing.
  const [expandedRuleId, setExpandedRuleId] = useState<string | null>(null)
  // The rule just created, lit briefly so the reader can find the row that
  // appeared "somewhere in the table".
  const [highlightRuleId, setHighlightRuleId] = useState<string | null>(null)
  useEffect(() => {
    if (!highlightRuleId) return
    const timer = window.setTimeout(() => setHighlightRuleId(null), NEW_RULE_HIGHLIGHT_MS)
    return () => window.clearTimeout(timer)
  }, [highlightRuleId])
  // Guided setup's hand-off: the header says "Step 3 of 3".
  const [guidedRule, setGuidedRule] = useState(false)

  const summaryQuery = useQuery({
    queryKey: monitorsSummaryKey(slug),
    queryFn: () => alertingApi.getMonitorsSummary(slug),
    enabled: !!slug,
    refetchInterval,
    staleTime: 30_000,
  })
  const summary = summaryQuery.data
  // Project-wide detection, read so a switched-off detector is said above the
  // rules it silences. Same key as Detection settings, so a change
  // there shows here without a reload. Its failure says nothing: the banner is
  // a warning, not a claim this section has to back.
  const detectionQuery = useQuery({
    queryKey: projectAnomalySettingsKey(slug),
    queryFn: () => anomalySettingsApi.get(slug),
    enabled: !!slug,
    meta: SILENT_ERROR_META,
  })
  const detectionOff = detectionQuery.data?.anomaly_detection_enabled === false
  const stateByRule = new Map<string, MonitorSummaryItem>(
    (summary?.monitors ?? []).map(monitor => [monitor.rule_id, monitor]),
  )
  // The stat tiles are toggles over the table below: "Firing 1" is
  // exactly the filter a reader wants, and the strip used to be a row of
  // numbers nothing could be done with. Pressing the lit tile again clears it.
  const [stateFilter, setStateFilter] = useState<MonitorStatus | null>(null)
  const toggleStateFilter = (status: MonitorStatus) =>
    setStateFilter(current => (current === status ? null : status))
  const visibleRules = stateFilter
    ? rules.filter(rule => stateByRule.get(rule.id)?.status === stateFilter)
    : rules

  // Same latch the destination card used, and for the same reason: the prop
  // stays set until the page clears it, so without this it would re-open the
  // form on the render right after the reader closed it. Adjusting state during
  // render is React's documented way to follow a prop.
  const [autoOpenConsumed, setAutoOpenConsumed] = useState(false)
  if (autoOpenRuleForDestinationId && !autoOpenConsumed) {
    setAutoOpenConsumed(true)
    setEditingRule(null)
    // Named after where it sends, so the last step does not open on an empty
    // required field. The reader can rename it.
    const targetName = autoOpenRuleDestinationName
      ?? destinations.find(destination => destination.id === autoOpenRuleForDestinationId)?.name
    setRuleForm({ ...defaultRuleForm(), name: targetName ? `Alerts to ${targetName}` : '' })
    setFormDestinationId(autoOpenRuleForDestinationId)
    setGuidedRule(true)
    setRuleDialogOpen(true)
  }

  /** Close the rule form and hand the guided-setup instruction back as spent. */
  const dismissRuleDialog = () => {
    setRuleDialogOpen(false)
    setEditingRule(null)
    setGuidedRule(false)
    onAutoOpenRuleConsumed()
  }

  // Every write goes through the one shared invalidation: a rule write also
  // moves the Inbox, the delivery log and this section's own summary.
  // Create and update render their error inside the dialog,
  // so they keep the global toast out of it.
  const createRuleMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => alertingApi.createRule(slug, formDestinationId, ruleFormToPayload(ruleForm)),
    onSuccess: created => {
      invalidateAlertingConfig(qc, slug)
      // Said, like a created destination is; the two halves of this
      // page used to behave differently.
      toast.success(`Rule "${created.name}" created`)
      setHighlightRuleId(created.id)
      dismissRuleDialog()
      setRuleForm(defaultRuleForm())
      // A created rule lands the alerting chapter's step — inert outside the
      // demo scenario (the reducer drops every other step).
      notifyStepCompleted('alerting/create-rule')
    },
  })

  const updateRuleMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => {
      if (!editingRule) throw new Error('Missing rule')
      return alertingApi.updateRule(
        slug,
        editingRule.destination_id,
        editingRule.id,
        ruleFormToPayload(ruleForm),
      )
    },
    onSuccess: saved => {
      invalidateAlertingConfig(qc, slug)
      toast.success(`Rule "${saved.name}" saved`)
      dismissRuleDialog()
      setRuleForm(defaultRuleForm())
    },
  })

  // The row-level writes below have no dialog to report in, and each used to
  // fail with nothing on screen: the switch snapped back, the bin did nothing.
  // They say why in a toast that keeps the global backstop's 401
  // silence, request reference and dedupe (`surfaceError`).
  const reportRowWriteError = (error: unknown) => surfaceError(error, stripValueErrorPrefix)

  const deleteRuleMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (rule: RuleWithDestination) =>
      alertingApi.deleteRule(slug, rule.destination_id, rule.id),
    onSuccess: (_, rule) => {
      invalidateAlertingConfig(qc, slug)
      toast.success(`Rule "${rule.name}" deleted`)
    },
    onError: reportRowWriteError,
  })

  // `checked` is the server's value, never local state, so a rejected write
  // cannot leave the switch showing a position the server refused. Pending is
  // scoped to the ONE rule being written: a shared flag disables the neighbours
  // for the duration of somebody else's request.
  const toggleRuleMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: ({ rule, enabled }: { rule: RuleWithDestination; enabled: boolean }) =>
      alertingApi.updateRule(slug, rule.destination_id, rule.id, { enabled }),
    onSuccess: () => invalidateAlertingConfig(qc, slug),
    onError: reportRowWriteError,
  })

  // Mute moved off the standalone monitor page onto the row. It writes the same
  // `muted_until` the destination card reads, so the two cannot disagree — they
  // are now the same screen.
  const muteMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: ({ rule, mutedUntil }: { rule: RuleWithDestination; mutedUntil: string | null }) =>
      mutedUntil === null
        ? alertingApi.unmuteMonitor(slug, rule.id)
        : alertingApi.muteMonitor(slug, rule.id, mutedUntil),
    onSuccess: () => invalidateAlertingConfig(qc, slug),
    onError: reportRowWriteError,
  })

  // The dialog's error is the previous attempt's; a fresh opening must not
  // show it. Reset on every close and every open, since the
  // guided-setup auto-open above cannot reset during render.
  const resetRuleMutations = () => {
    createRuleMut.reset()
    updateRuleMut.reset()
  }

  const closeRuleDialog = () => {
    dismissRuleDialog()
    resetRuleMutations()
  }

  // Picking another destination keeps the message format only when that
  // channel supports it. `slack_mrkdwn` carried over to a Telegram destination
  // was a 422 on Create, over a format Select that rendered blank.
  const changeFormDestination = (destinationId: string) => {
    setFormDestinationId(destinationId)
    const type = destinations.find(destination => destination.id === destinationId)?.type
    if (!type) return
    setRuleForm(current => {
      const messageFormat = messageFormatForDestination(current.message_format, type)
      return messageFormat === current.message_format
        ? current
        : withMessageFormat(current, messageFormat)
    })
  }

  const openNewRule = () => {
    resetRuleMutations()
    openBlankRuleForm(null)
  }

  // `metricId` scopes the new rule to one catalog metric: metrics on, the
  // other scopes off, and a metric filter naming it (#246).
  function openBlankRuleForm(metricId: string | null) {
    setEditingRule(null)
    setRuleForm(
      metricId
        ? {
            ...defaultRuleForm(),
            include_project_total: false,
            include_event_types: false,
            include_events: false,
            include_metrics: true,
            filters: [{ uid: makeFilterUid(), field: 'metric', operator: 'in', values: [metricId] }],
          }
        : defaultRuleForm(),
    )
    // Prefill only when there is no choice to make: exactly one ENABLED
    // destination — a disabled one next to it is not a real choice.
    // With several, the picker starts empty and Create names it on submit,
    // rather than silently routing to whichever sorted first.
    const enabled = destinations.filter(destination => destination.enabled)
    setFormDestinationId(
      enabled.length === 1
        ? (enabled[0]?.id ?? '')
        : destinations.length === 1 ? (destinations[0]?.id ?? '') : '',
    )
    setRuleDialogOpen(true)
  }

  // `?new=rule` opens the create form on arrival — the inbox's "Create a rule"
  // and a metric page's "Create alert…" (`&metric=<id>`) link
  // here. Latched during render like the guided-setup hand-off above, then the
  // params are dropped so Back or a refresh does not reopen it.
  const newRuleRequested = searchParams.get('new') === 'rule'
  const newRuleMetricId = searchParams.get('metric')
  const [newRuleConsumed, setNewRuleConsumed] = useState(false)
  if (newRuleRequested && canWrite && !newRuleConsumed) {
    setNewRuleConsumed(true)
    openBlankRuleForm(newRuleMetricId)
  } else if (!newRuleRequested && newRuleConsumed) {
    // Spent and stripped: the next `?new=rule` opens the form again.
    setNewRuleConsumed(false)
  }
  useEffect(() => {
    if (!newRuleRequested) return
    setSearchParams(
      current => {
        const params = new URLSearchParams(current)
        params.delete('new')
        params.delete('metric')
        return params
      },
      { replace: true },
    )
  }, [newRuleRequested, setSearchParams])

  const openEditRule = (rule: RuleWithDestination) => {
    resetRuleMutations()
    setEditingRule(rule)
    setRuleForm(ruleToForm(rule))
    setFormDestinationId(rule.destination_id)
    setRuleDialogOpen(true)
  }

  const handleDeleteRule = async (rule: RuleWithDestination) => {
    const ok = await confirm({
      title: 'Delete alert rule',
      message: `Delete "${rule.name}"? ${describeDeletionImpact(rule.total_deliveries, rule.incident_count)}`,
      confirmLabel: 'Delete',
      variant: 'danger',
    })
    // Not while a delete is already in flight: a second confirm used to fire a
    // second DELETE for the same rule.
    if (ok && !deleteRuleMut.isPending) deleteRuleMut.mutate(rule)
  }

  const ruleMutation = editingRule ? updateRuleMut : createRuleMut
  const hasDestinations = destinations.length > 0
  // Through the shared count helper, like the sibling audit panel. This was
  // `${rules.length} routing` — a count with its noun missing, which reads as an
  // unfinished template sitting directly on top of a table of numbers.
  // "Alert rule", the one name for this object: the tab, the button,
  // the dialog and this count used to say Monitors, rule, alert rule and
  // routing rule.
  const rulesSubtitle = rules.length === 0
    ? undefined
    : stateFilter
      ? `${visibleRules.length} of ${countOf(rules.length, 'alert rule', 'alert rules')} · ${STATE_FILTER_LABEL[stateFilter].toLowerCase()}`
      : countOf(rules.length, 'alert rule', 'alert rules')
  const destinationById = new Map(destinations.map(destination => [destination.id, destination]))

  return (
    <>
      {dialog}

      {/* Once, above everything this section can no longer offer to change —
          rather than a tooltip on each of the switches and bins that are simply
          absent below. */}
      {!canWrite && <ReadOnlyNotice>{VIEWER_READ_ONLY_NOTICE}</ReadOnlyNotice>}
      {detectionOff && (
        <p
          role="status"
          className="m-0 flex flex-wrap items-start gap-2 rounded-card border px-3 py-2.5 text-body-sm border-warning bg-warning-soft"
        >
          <TriangleAlert aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-warning" />
          <span className="min-w-0 flex-1">{DETECTION_OFF_MESSAGE}</span>
          <Link to={projectPath(currentOrgSlug(), slug, '/settings/monitoring')} className="underline underline-offset-2">
            Detection settings
          </Link>
        </p>
      )}

      {/* Hidden entirely when nothing is configured, so an all-zero
          FIRING/WARNING/HEALTHY row never sits above the empty state. */}
      {rules.length > 0 && (
        <MiniStatStrip boxed>
          {/* A skeleton, not "—" or a green "Healthy", until the summary
              answers: a count the query has not returned is not a count,
              and a green tone on it is a false all-clear. */}
          {/* Each state tile is a toggle once the summary has answered: a
              filter over counts nobody has seen yet would hide rows on a
              guess. */}
          <MiniStat
            label="Firing"
            value={summary ? formatNumber(summary.firing_count) : <StatValueSkeleton />}
            tone={summary && summary.firing_count > 0 ? 'danger' : 'neutral'}
            pulse={!!summary && summary.firing_count > 0}
            delta={summary && summary.firing_count > 0 ? 'now' : undefined}
            onPress={summary ? () => toggleStateFilter('firing') : undefined}
            pressed={stateFilter === 'firing'}
            title="Show only firing rules"
          />
          <MiniStat
            label="Warning"
            value={summary ? formatNumber(summary.warning_count) : <StatValueSkeleton />}
            tone={summary && summary.warning_count > 0 ? 'warning' : 'neutral'}
            onPress={summary ? () => toggleStateFilter('warning') : undefined}
            pressed={stateFilter === 'warning'}
            title="Show only rules in warning"
          />
          {/* Neutral: a green figure beside two grey zeros made the one
              count that needs nothing the loudest tile on the strip.
              Colour is for Firing and Warning, and only when non-zero. */}
          <MiniStat
            label="Healthy"
            value={summary ? formatNumber(summary.healthy_count) : <StatValueSkeleton />}
            onPress={summary ? () => toggleStateFilter('healthy') : undefined}
            pressed={stateFilter === 'healthy'}
            title="Show only healthy rules"
          />
          <MiniStat label="Rules" value={formatNumber(rules.length)} />
        </MiniStatStrip>
      )}

      <Panel
        title="Rules"
        subtitle={rulesSubtitle}
        right={
          canWrite && hasDestinations && rules.length > 0 ? (
            // Exactly one coach mark, and only where a demo can act on it: the
            // local sink is the destination whose deliveries never leave the
            // instance. It followed the rule form out of the destination card.
            <ScenarioCoachMark
              step="alerting/create-rule"
              when={destinations.some(destination => destination.is_local)}
            >
              {/* Marked for the `c` shortcut, which otherwise looks for "New …". */}
              <Button size="sm" variant="outline" onClick={openNewRule} data-create-action="">
                <Plus className="mr-2 h-4 w-4" />
                Add rule
              </Button>
            </ScenarioCoachMark>
          ) : undefined
        }
      >
        {rules.length === 0 ? (
          <div className="py-6">
            <EmptyState
              icon={Bell}
              title="No rules yet"
              description={
                hasDestinations
                  ? 'tripl already watches every event’s rhythm and raises signals on spikes and drops. A rule decides which of those signals matter and where they go.'
                  : 'A rule delivers to a destination, and this project has none yet. Add a channel first, then route signals to it.'
              }
              action={
                !canWrite
                  ? undefined
                  : hasDestinations
                    ? (
                        <Button size="sm" onClick={openNewRule}>
                          <Plus className="mr-2 h-4 w-4" />
                          Add rule
                        </Button>
                      )
                    : (
                        <Button size="sm" variant="outline" onClick={onGoToDestinations}>
                          Add a destination
                        </Button>
                      )
              }
            />
          </div>
        ) : (
          <div>
            <div role="table" aria-label="Alert rules">
              {/* The header is a table's; below `md` each row is a card that
                  carries its own labels, so there is nothing to head. */}
              <div role="rowgroup" className="hidden md:block">
                <div
                  role="row"
                  className={`${ruleGridClass(canWrite)} border-b py-2 micro-label border-border-subtle text-fg-tertiary`}
                >
                  <span role="columnheader">Rule</span>
                  <span role="columnheader">Condition</span>
                  <span role="columnheader" className="hidden lg:block">Routes to</span>
                  <span role="columnheader">State</span>
                  <span role="columnheader">Last fired</span>
                  <span role="columnheader" className="text-right">Actions</span>
                </div>
              </div>
              <div role="rowgroup">
                {stateFilter && visibleRules.length === 0 && (
                  <div role="row">
                    <p role="cell" className="flex flex-wrap items-center gap-2 py-4 text-body-sm text-fg-tertiary">
                      No rules are {STATE_FILTER_LABEL[stateFilter].toLowerCase()} right now.
                      <Button type="button" variant="link" size="sm" className="h-auto p-0" onClick={() => setStateFilter(null)}>
                        Show all rules
                      </Button>
                    </p>
                  </div>
                )}
                {visibleRules.map(rule => (
                  <RuleRow
                    key={rule.id}
                    slug={slug}
                    rule={rule}
                    destination={destinationById.get(rule.destination_id)}
                    highlighted={highlightRuleId === rule.id}
                    state={stateByRule.get(rule.id)}
                    scans={scans}
                    scansLoaded={scansLoaded}
                    scansFailed={scansFailed}
                    expanded={expandedRuleId === rule.id}
                    onToggleExpanded={() =>
                      setExpandedRuleId(current => (current === rule.id ? null : rule.id))
                    }
                    canWrite={canWrite}
                    isTogglePending={
                      toggleRuleMut.isPending && toggleRuleMut.variables?.rule.id === rule.id
                    }
                    isMutePending={muteMut.isPending && muteMut.variables?.rule.id === rule.id}
                    isDeletePending={deleteRuleMut.isPending && deleteRuleMut.variables?.id === rule.id}
                    onToggle={enabled => toggleRuleMut.mutate({ rule, enabled })}
                    onMute={mutedUntil => muteMut.mutate({ rule, mutedUntil })}
                    onReplay={() => { setReplayDraft(null); setReplayingRule(rule) }}
                    onEdit={() => openEditRule(rule)}
                    onDelete={() => void handleDeleteRule(rule)}
                  />
                ))}
              </div>
            </div>
          </div>
        )}
      </Panel>

      {/* Gated on the role as well as on the open flag: `refresh()` can rewrite
          the session mid-visit, and an editor form left open across a demotion
          would still submit its Save. */}
      <RuleEditorDialog
        open={canWrite && ruleDialogOpen}
        onClose={closeRuleDialog}
        slug={slug}
        destinations={destinations}
        destinationId={formDestinationId}
        onDestinationIdChange={changeFormDestination}
        isEditing={!!editingRule}
        ruleForm={ruleForm}
        setRuleForm={setRuleForm}
        eventTypes={eventTypes}
        scans={scans}
        scansLoaded={scansLoaded}
        scansFailed={scansFailed}
        // Both replays from inside the editor: the rule on file, and
        // the rule with this form's edits laid over it server-side, unsaved.
        onReplaySaved={editingRule ? () => { setReplayDraft(null); setReplayingRule(editingRule) } : undefined}
        onReplayDraft={
          editingRule
            ? () => { setReplayDraft(ruleFormToPayload(ruleForm)); setReplayingRule(editingRule) }
            : undefined
        }
        // Rides the monitors-summary response this section already polls, so the
        // editor gains the fact without a second request. Undefined until that
        // request answers, which the dialog reads as "say nothing yet".
        scopeReadiness={summary?.scope_readiness}
        guidedStep={guidedRule && !editingRule}
        onSubmit={() => ruleMutation.mutate()}
        isPending={ruleMutation.isPending}
        isError={ruleMutation.isError}
        error={ruleMutation.error}
      />

      {replayingRule && (
        <RuleReplayDialog
          open={!!replayingRule}
          onOpenChange={value => { if (!value) { setReplayingRule(null); setReplayDraft(null) } }}
          slug={slug}
          destinationId={replayingRule.destination_id}
          rule={replayingRule}
          scans={scans}
          draft={replayDraft}
        />
      )}
    </>
  )
}

interface RuleRowProps {
  slug: string
  rule: RuleWithDestination
  /** The destination the rule hangs off; its type names the channel. */
  destination: AlertDestination | undefined
  /** Just created: lit briefly and scrolled into view. */
  highlighted: boolean
  state: MonitorSummaryItem | undefined
  scans: ScanConfig[]
  scansLoaded: boolean
  scansFailed: boolean
  expanded: boolean
  onToggleExpanded: () => void
  canWrite: boolean
  isTogglePending: boolean
  isMutePending: boolean
  isDeletePending: boolean
  onToggle: (enabled: boolean) => void
  onMute: (mutedUntil: string | null) => void
  onReplay: () => void
  onEdit: () => void
  onDelete: () => void
}

function RuleRow({
  slug,
  rule,
  destination,
  highlighted,
  state,
  scans,
  scansLoaded,
  scansFailed,
  expanded,
  onToggleExpanded,
  canWrite,
  isTogglePending,
  isMutePending,
  isDeletePending,
  onToggle,
  onMute,
  onReplay,
  onEdit,
  onDelete,
}: RuleRowProps) {
  const tone = state ? STATUS_TONE[state.status] : 'neutral'
  const settingsId = `rule-settings-${rule.id}`
  // Built from the rule itself, not from the summary: the condition is
  // configuration, so it renders correctly while the state request is still in
  // flight. Sans and in words, with what it watches underneath, so a
  // metrics-only rule no longer reads like every other rule. The
  // shared cooldown formatter keeps one rule one duration on every screen.
  const { condition, watches } = ruleConditionSummary(rule)
  const destinationType = destination?.type ?? state?.destination_type
  const channel = destinationType ? channelLabel(destinationType) : null

  const rowRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    // `?.`: jsdom has no scrollIntoView, and a highlight is not worth a throw.
    if (highlighted) rowRef.current?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' })
  }, [highlighted])

  return (
    <>
    <div
      ref={rowRef}
      role="row"
      className={`${ruleGridClass(canWrite)} flex min-h-(--row-h) flex-wrap items-center gap-x-3 gap-y-2 border-b py-3 transition-colors duration-700 last:border-0 md:py-2.5 ${highlighted ? 'bg-accent-soft' : ''} border-border-subtle`}
      data-highlighted={highlighted || undefined}
    >
      {/* Nothing in this table ellipsizes but the destination name. Every text
          cell carried `truncate`, and at the width this panel actually gets, a
          two-row table cut five separate strings at once — including
          CONDITION, which is the whole payload of the row. Wrapping costs a
          row a line; an ellipsis costs the fact. */}
      <span role="cell" className="flex min-w-0 basis-full flex-col gap-1 md:basis-auto">
        {/* One line: the chevron, the dot and the name never wrap apart, so
            the dot cannot sit alone on a line above its rule. */}
        <span className="flex min-w-0 flex-nowrap items-center gap-1.5">
          {/* Expands the settings this list does not have room for. `Settings`
              names what opens, not the widget — a bare chevron says nothing
              about what is behind it. */}
          <button
            type="button"
            onClick={onToggleExpanded}
            aria-expanded={expanded}
            // Only while the row it names is in the document: an
            // aria-controls pointing at nothing is a broken reference.
            aria-controls={expanded ? settingsId : undefined}
            aria-label={`${expanded ? 'Hide' : 'Show'} settings for ${rule.name}`}
            className="shrink-0 rounded-sm p-0.5 transition-colors hover:bg-[var(--surface-hover)] text-fg-tertiary"
          >
            {expanded
              ? <ChevronDown aria-hidden="true" className="h-3.5 w-3.5" />
              : <ChevronRight aria-hidden="true" className="h-3.5 w-3.5" />}
          </button>
          {/* Static in a list: the Firing count above pulses once;
              every row pulsing with it made the table shimmer. */}
          <Dot tone={tone} size={7} className="shrink-0" />
          {/* The detail page is the rule's fired history — the one thing
              neither this row nor its expansion can carry. */}
          <Link
            to={projectPath(currentOrgSlug(), slug, `/monitors/${rule.id}`)}
            className="min-w-0 break-words text-body-sm font-medium no-underline hover:underline text-fg"
          >
            {rule.name}
          </Link>
          {!rule.enabled && <Chip tone="neutral" size="xs" className="shrink-0">off</Chip>}
        </span>
        {/* A rule's mute is a STATE, shown as one — BellOff on every unmuted
            row read as "muted". The only mute ACTION is in the menu.

            The MIRROR IMAGE of the incident card's muted line in
            AlertingInbox.tsx: an INCIDENT has three mute states, an ALERT RULE
            two, because `is_rule_muted()` (backend `_alerting_monitors.py`)
            returns FALSE the moment `muted_until` is NULL. So `muted &&
            !muted_until` cannot occur here; the second condition is type
            narrowing, not a second case. Do not add a "muted, no end" branch —
            a rule's permanent lever is its `enabled` switch. */}
        {rule.muted && rule.muted_until && (
          <span className="pl-6">
            <Chip tone="warning" size="xs" icon={<BellOff aria-hidden="true" className="size-3" />}>
              {`muted until ${formatDateTime(rule.muted_until)}`}
            </Chip>
          </span>
        )}
        {/* Delivery health. `Never delivered` is a different fact from `last
            sent 3h ago`, and it followed the rule off the destination card
            rather than being dropped in the move. */}
        <span className="break-words pl-6 text-caption text-fg-tertiary">
          {rule.total_deliveries === 0 ? (
            'Never delivered'
          ) : (
            <>
              {countOf(rule.total_deliveries, 'delivery', 'deliveries')}
              {' · '}
              {countOf(rule.incident_count, 'incident', 'incidents')}
              {' · last '}
              {formatRelativeTime(rule.last_delivery_at)}
              {rule.last_delivery_status ? ` · ${rule.last_delivery_status}` : ''}
            </>
          )}
        </span>
        {/* Where it routes, until `lg` gives it a column of its own. */}
        <span className="flex min-w-0 items-center gap-1 pl-6 text-caption lg:hidden text-fg-tertiary">
          {destinationType && <ChannelGlyph type={destinationType} aria-hidden="true" className="size-3 shrink-0" />}
          <span className="truncate">{`Routes to ${rule.destination_name}${channel ? ` (${channel})` : ''}`}</span>
        </span>
      </span>
      <span role="cell" className="flex min-w-0 basis-full flex-col gap-0.5 pl-6 md:basis-auto md:pl-0">
        <span className="tnum break-words text-body-sm text-fg">{condition}</span>
        <span className="break-words text-caption text-fg-tertiary">{watches}</span>
      </span>
      <span role="cell" className="hidden min-w-0 items-center gap-1.5 lg:flex">
        {/* The channel's icon rather than a raw `demo_sink` chip, and the
            name on one line. */}
        {destinationType && (
          <ChannelGlyph
            type={destinationType}
            aria-label={channel ?? undefined}
            role="img"
            className="size-3.5 shrink-0"
            style={{ color: 'var(--fg-subtle)' }}
          />
        )}
        <span
          className="truncate text-caption text-fg-tertiary"
          title={`Routes to the "${rule.destination_name}" destination`}
        >
          {rule.destination_name}
        </span>
      </span>
      {/* On a phone, state, last fired and the actions share the card's
          last line. */}
      <span role="cell" className="pl-6 md:pl-0">
        {state ? (
          <Chip tone={tone} size="xs">{STATUS_LABEL[state.status]}</Chip>
        ) : (
          <span className="text-caption text-fg-tertiary">—</span>
        )}
      </span>
      <span role="cell" className="tnum pl-6 text-caption md:pl-0 text-fg-tertiary">
        {state?.last_anomaly_at
          ? <><span className="md:hidden">Last fired </span>{formatRelativeTime(state.last_anomaly_at)}</>
          : '—'}
      </span>
      <span role="cell" className="ml-auto flex shrink-0 items-center justify-end gap-1.5">
        {canWrite && (
          <Switch
            checked={rule.enabled}
            disabled={isTogglePending}
            onCheckedChange={onToggle}
            aria-label={`Toggle ${rule.name}`}
          />
        )}
        {canWrite && (
          <IconButton
            variant="ghost"
            label={`Edit rule ${rule.name}`}
            onClick={onEdit}
          >
            <Pencil aria-hidden="true" />
          </IconButton>
        )}
        {/* Exactly one rule coaches the simulate step: the seeded firing rule,
            whose window is guaranteed to hold anomalies. Replay sits in the
            editor's menu, so the mark points at the menu. The span is the
            anchor: the mark clones its ref onto its child, and RuleActionsMenu
            takes none, so the step read as "not visible" beside the menu. */}
        <ScenarioCoachMark
          step="alerting/simulate"
          when={rule.name === SCENARIO_SEEDED.firingRuleName}
        >
          <span className="inline-flex">
            {canWrite ? (
              <RuleActionsMenu
                ruleName={rule.name}
                muted={rule.muted}
                isMutePending={isMutePending}
                isDeletePending={isDeletePending}
                deleteImpact={describeDeletionImpact(rule.total_deliveries, rule.incident_count)}
                onMute={onMute}
                onReplay={onReplay}
                onDelete={onDelete}
              />
            ) : (
              // Replay stays for everyone: it is the one control here the API
              // does not gate, because it saves nothing (backend alerting.py has
              // no EditorUserDep on /simulate). A viewer has nothing else to put
              // in a menu, so it is a labelled button.
              <Button
                variant="ghost"
                size="sm"
                onClick={onReplay}
                aria-label={`Replay ${rule.name}`}
              >
                <History aria-hidden="true" />
                Replay
              </Button>
            )}
          </span>
        </ScenarioCoachMark>
      </span>
    </div>

    {expanded && (
      <div
        role="row"
        id={settingsId}
        aria-label={`Settings for ${rule.name}`}
        className="border-b px-4 py-3 last:border-0 border-border-subtle bg-bg-sunken"
      >
        {/* Every setting labelled, because the whole block used to be a single
            wrapped run of unlabelled spans in which no individual value could be
            found without reading all of them.

            One cell spanning all six columns, and a wrapper around the list
            rather than `role="cell"` ON the <dl>: that role replaced the list's
            own, which left every <dt>/<dd> without the parent they require,
            and a one-cell row in a six-column table was announced as sitting
            under "Rule" alone. */}
        <div role="cell" aria-colspan={6}>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3 lg:grid-cols-4">
          <RuleSetting
            label="Scan"
            value={scanSettingLabel(rule.scan_config_id, scans, scansLoaded, scansFailed)}
          />
          <RuleSetting label="Scopes" value={scopeSummary(rule) || 'none'} />
          <RuleSetting label="Direction" value={directionSummary(rule) || 'none'} />
          <RuleSetting label="Cooldown" value={formatCooldown(rule.cooldown_minutes)} />
          <RuleSetting label="Min %" value={String(rule.min_percent_delta)} />
          <RuleSetting label="Min Δ" value={String(rule.min_absolute_delta)} />
          <RuleSetting label="Min expected" value={String(rule.min_expected_count)} />
          <RuleSetting
            label="Message"
            value={!rule.message_template || isDefaultMessageTemplate(rule.message_template, rule.message_format)
              ? `default (${rule.message_format})`
              : `custom (${rule.message_format})`}
          />
          {rule.notify_owners && (
            <RuleSetting label="Owners" value="also emailed" />
          )}
          {!!rule.filters.length && (
            <RuleSetting
              label="Filters"
              value={countOf(rule.filters.length, 'filter', 'filters')}
            />
          )}
        </dl>
        </div>
      </div>
    )}
    </>
  )
}

/**
 * The scan a rule is bound to, as the settings row names it.
 *
 * Three states, not two: while the scan list is still loading the name is not
 * known YET, and printing "unknown scan" then was indistinguishable from the
 * scan having been deleted.
 */
function scanSettingLabel(
  scanConfigId: string | null,
  scans: ScanConfig[],
  scansLoaded: boolean,
  scansFailed: boolean,
): string {
  if (!scanConfigId) return 'all scans'
  const name = scans.find(scan => scan.id === scanConfigId)?.name
  if (name !== undefined) return name
  if (scansLoaded) return 'unknown scan'
  // A failed list will not answer on its own; "…" would read as loading forever.
  return scansFailed ? 'scan unavailable' : '…'
}

/** One rule setting, labelled. */
function RuleSetting({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="micro-label text-fg-tertiary">{label}</dt>
      <dd className="break-words text-body-sm text-foreground">{value}</dd>
    </div>
  )
}

/**
 * Replay, Mute and Delete, labelled, behind one "…".
 *
 * The row used to show five unlabelled icons, and Mute was a bell-with-slash
 * on every unmuted rule — which reads as "this is muted". Its durations opened
 * inline and pushed the whole row's columns sideways; in a menu they
 * take no room at all.
 *
 * The mute choices keep the Inbox's labels, "Mute <target> for <duration>",
 * because they are the same FUNCTIONS: `muteChoiceName` and `unmuteName` come
 * from `@/lib/mutePresets` alongside the durations, and only the
 * fixed durations are offered — a NULL `muted_until` UN-mutes a rule.
 */
function RuleActionsMenu({
  ruleName,
  muted,
  isMutePending,
  isDeletePending,
  deleteImpact,
  onMute,
  onReplay,
  onDelete,
}: {
  ruleName: string
  muted: boolean
  isMutePending: boolean
  isDeletePending: boolean
  /** The shared cascade sentence, so the menu and its confirm count alike. */
  deleteImpact: string
  onMute: (mutedUntil: string | null) => void
  onReplay: () => void
  onDelete: () => void
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <IconButton variant="ghost" label={`More actions for ${ruleName}`}>
          <MoreHorizontal aria-hidden="true" />
        </IconButton>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" sideOffset={6} className="w-56">
        {/* The simulate step's second gesture: its ring is on this menu's
            trigger, and the item it opens onto was left for the visitor to find. */}
        <ScenarioCoachMark
          step="alerting/simulate"
          followUp
          when={ruleName === SCENARIO_SEEDED.firingRuleName}
          tag="Then click here"
          side="left"
        >
          <DropdownMenuItem onSelect={onReplay} aria-label={`Replay ${ruleName}`}>
            <History aria-hidden="true" />
            Replay
          </DropdownMenuItem>
        </ScenarioCoachMark>
        <DropdownMenuSeparator />
        {muted ? (
          <DropdownMenuItem
            disabled={isMutePending}
            onSelect={() => onMute(null)}
            aria-label={unmuteName(ruleName)}
          >
            <Bell aria-hidden="true" />
            Unmute
          </DropdownMenuItem>
        ) : (
          <>
            <DropdownMenuLabel className="text-caption font-normal text-fg-subtle">Mute for</DropdownMenuLabel>
            {MUTE_PRESETS.map(preset => (
              <DropdownMenuItem
                key={preset.label}
                disabled={isMutePending}
                // MUTE_PRESETS' `ms` is `number`, so this call is statically
                // confined to the "for <duration>" branch of `muteChoiceName`.
                aria-label={muteChoiceName(ruleName, preset)}
                onSelect={() => onMute(muteUntilIso(preset.ms))}
              >
                <BellOff aria-hidden="true" />
                {preset.label}
              </DropdownMenuItem>
            ))}
          </>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem
          variant="destructive"
          disabled={isDeletePending}
          aria-label={`Delete rule ${ruleName}`}
          onSelect={onDelete}
        >
          <Trash2 aria-hidden="true" />
          <span className="grid">
            Delete rule…
            <span className="text-caption text-fg-subtle">{deleteImpact}</span>
          </span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
