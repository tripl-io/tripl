/**
 * The coached demo scenario — pure model, v3: chapters (tripl-odrj.4).
 *
 * v1 (tripl-2su6.21.1) was a single four-step chain. v2 turns the scenario into
 * an ordered set of CHAPTERS — live-loop, edit-event, variables, branches,
 * reconcile, alerting, explore — each a short step chain over one product area.
 * One chapter is active at a time; per-chapter progress, dismissal and restart
 * are all first-class, and the strip offers the next chapter when one lands.
 *
 * Completion semantics differ by chapter, deliberately:
 * - live-loop keeps its artifact-bound watches (the runtime tick manufactures
 *   real jobs constantly, so only the artifact the user's own action produced
 *   can advance it — see the v1 rationale below).
 * - every other chapter advances on DIRECT notifications: either a route visit
 *   (deep-link steps, the explore chapter) or a `stepCompleted` fired from the
 *   exact mutation the user performed (save event, post comment, accept drift…).
 *   No polling is added for them.
 *
 * The persisted shape shares the v1 localStorage key. Valid v1/v2 blobs migrate
 * forward; v2 edit-event progress restarts because its deterministic target
 * changed. Unknown/malformed versions read fresh. Kept free of React so every
 * transition is unit-testable without rendering.
 */

import { getMetricMonitoringPath } from '@/lib/monitoring'
import { SCENARIO_STORAGE_PREFIX } from './demoLocalState'
import { currentOrgSlug, projectPath } from '@/lib/navigation'
import { orgStorageKey } from '@/lib/activeOrg'

export type ChapterId =
  | 'live-loop'
  | 'edit-event'
  | 'variables'
  | 'branches'
  | 'reconcile'
  | 'alerting'
  | 'explore'

export const CHAPTER_IDS: readonly ChapterId[] = [
  'live-loop',
  'edit-event',
  'variables',
  'branches',
  'reconcile',
  'alerting',
  'explore',
]

export type ChapterStatus = 'not_started' | 'active' | 'completed' | 'dismissed'

/** Step ids are chapter-scoped ('chapter/step') so one flat set covers the app. */
export type ScenarioStepId =
  | 'live-loop/run-scan'
  | 'live-loop/watch-scan'
  | 'live-loop/collect-metric'
  | 'live-loop/see-chart'
  | 'edit-event/open-editor'
  | 'edit-event/set-value'
  | 'edit-event/set-token'
  | 'edit-event/save'
  | 'variables/open-variables'
  | 'variables/inspect-values'
  | 'variables/see-drift'
  | 'branches/open-branches'
  | 'branches/open-branch'
  | 'branches/review-diff'
  | 'branches/comment'
  | 'reconcile/open-reconciliation'
  | 'reconcile/accept-shadow'
  | 'reconcile/review-drift'
  | 'alerting/open-alerting'
  | 'alerting/create-rule'
  | 'alerting/simulate'
  | 'explore/visit-coverage'
  | 'explore/visit-anomaly'
  | 'explore/use-search'

export const CHAPTER_STEP_IDS: Record<ChapterId, readonly [ScenarioStepId, ...ScenarioStepId[]]> = {
  'live-loop': [
    'live-loop/run-scan',
    'live-loop/watch-scan',
    'live-loop/collect-metric',
    'live-loop/see-chart',
  ],
  'edit-event': [
    'edit-event/open-editor',
    'edit-event/set-value',
    'edit-event/set-token',
    'edit-event/save',
  ],
  variables: ['variables/open-variables', 'variables/inspect-values', 'variables/see-drift'],
  branches: [
    'branches/open-branches',
    'branches/open-branch',
    'branches/review-diff',
    'branches/comment',
  ],
  reconcile: ['reconcile/open-reconciliation', 'reconcile/accept-shadow', 'reconcile/review-drift'],
  alerting: ['alerting/open-alerting', 'alerting/create-rule', 'alerting/simulate'],
  explore: ['explore/visit-coverage', 'explore/visit-anomaly', 'explore/use-search'],
}

const ALL_STEP_IDS: readonly ScenarioStepId[] = CHAPTER_IDS.flatMap(
  (chapter) => CHAPTER_STEP_IDS[chapter],
)

/** The chapter a step id belongs to. Step ids are 'chapter/step' by contract. */
export function stepChapter(step: ScenarioStepId): ChapterId {
  return step.slice(0, step.indexOf('/')) as ChapterId
}

export type ScenarioStatus = ChapterStatus

/** Why a chapter went backwards. Surfaced as copy, never as a silent reset. */
export type ScenarioHint = 'scan-failed' | 'scan-stale' | 'collect-failed' | 'watch-timeout'

const CHAPTER_STATUSES: readonly ChapterStatus[] = [
  'not_started',
  'active',
  'completed',
  'dismissed',
]

const SCENARIO_HINTS: readonly ScenarioHint[] = [
  'scan-failed',
  'scan-stale',
  'collect-failed',
  'watch-timeout',
]

/** Human copy for each regression, so the strip never leaves the user guessing. */
export const SCENARIO_HINT_COPY: Record<ScenarioHint, string> = {
  'scan-failed': 'That run failed. Run the scan again to carry on.',
  'scan-stale': 'That run is gone — the demo was reset. Start with a fresh scan.',
  'collect-failed': 'That collection failed. Try collecting the metric again.',
  'watch-timeout': 'That run is taking longer than usual. Check on it, or start it again.',
}

/** The ScanJob the user's own "Run" produced — the only job live-loop trusts. */
export interface ScenarioScanArtifact {
  scanConfigId: string
  scanJobId: string
  startedAt: number
}

/** The metric the user chose to collect, and when they fired it. */
export interface ScenarioMetricArtifact {
  metricId: string
  startedAt: number
}

/**
 * Per-chapter progress. `step` is the step the user is on while active, and the
 * last step reached otherwise. Artifacts are a flat string record so the shape
 * stays chapter-agnostic; live-loop's typed artifacts are encoded into it via
 * the helpers below.
 */
export interface ChapterState {
  status: ChapterStatus
  step: ScenarioStepId
  artifacts?: Record<string, string>
  hint?: ScenarioHint
}

/**
 * The persisted shape. `v` is checked on read: v1/v2 migrate forward; any other
 * unknown version reads fresh.
 */
export interface ScenarioState {
  v: 3
  activeChapter: ChapterId | null
  chapters: Partial<Record<ChapterId, ChapterState>>
  /**
   * Set once the user starts or restarts a chapter themselves. The initial
   * state is already on live-loop's first step, so the step alone cannot tell
   * a pristine scenario from one the user chose (LIVE-9's strip rule).
   */
  engaged?: true
}

export type ScenarioEvent =
  | { type: 'startChapter'; chapter: ChapterId }
  | { type: 'restartChapter'; chapter: ChapterId }
  | { type: 'dismissChapter'; chapter: ChapterId }
  /** A notify-driven or visit-driven step landed. Ignored unless it is the
   *  active chapter's CURRENT step, so stray notifies can never skip ahead.
   *  `path` is the pathname that completed a visit-driven step, when one did —
   *  edit-event remembers it so later steps can deep-link back into the editor. */
  | { type: 'stepCompleted'; step: ScenarioStepId; path?: string }
  | { type: 'scanRunStarted'; scanConfigId: string; scanJobId: string; at: number }
  | { type: 'scanSettled'; outcome: 'completed' | 'failed' | 'cancelled' | 'stale' }
  | { type: 'collectStarted'; metricId: string; at: number }
  | { type: 'collectSettled'; outcome: 'success' | 'error' }
  | { type: 'chartVisited'; metricId: string }
  | { type: 'watchTimedOut' }

function freshChapterState(chapter: ChapterId): ChapterState {
  return { status: 'active', step: CHAPTER_STEP_IDS[chapter][0] }
}

/** A fresh scenario opens on the first chapter, exactly as v1 opened active. */
export function initialScenarioState(): ScenarioState {
  return {
    v: 3,
    activeChapter: 'live-loop',
    chapters: { 'live-loop': freshChapterState('live-loop') },
  }
}

export function chapterStatus(state: ScenarioState, chapter: ChapterId): ChapterStatus {
  return state.chapters[chapter]?.status ?? 'not_started'
}

export function activeChapterState(state: ScenarioState): ChapterState | undefined {
  return state.activeChapter ? state.chapters[state.activeChapter] : undefined
}

/** True while a chapter is running — the strip's "coach me" condition. */
export function isScenarioActive(state: ScenarioState): boolean {
  return activeChapterState(state)?.status === 'active'
}

/** Zero-based position of the active chapter's step, for progress segments. */
export function scenarioStepIndex(state: ScenarioState): number {
  const chapter = state.activeChapter
  const chapterState = activeChapterState(state)
  if (!chapter || !chapterState) return 0
  return Math.max(0, CHAPTER_STEP_IDS[chapter].indexOf(chapterState.step))
}

/**
 * The chapter to offer once one completes: the next in order that has not been
 * completed, wrapping past the end. Null when every chapter is done.
 */
export function nextChapterId(state: ScenarioState): ChapterId | null {
  const from = state.activeChapter ? CHAPTER_IDS.indexOf(state.activeChapter) : -1
  for (let offset = 1; offset <= CHAPTER_IDS.length; offset += 1) {
    const candidate = CHAPTER_IDS[(from + offset) % CHAPTER_IDS.length]
    if (candidate === undefined || candidate === state.activeChapter) continue
    if (chapterStatus(state, candidate) !== 'completed') return candidate
  }
  return null
}

/** live-loop's typed scan artifact, decoded from the flat artifact record. */
export function scenarioScanArtifact(state: ScenarioState): ScenarioScanArtifact | undefined {
  const artifacts = state.chapters['live-loop']?.artifacts
  if (!artifacts) return undefined
  const { scanConfigId, scanJobId, scanStartedAt } = artifacts
  if (!scanConfigId || !scanJobId || !scanStartedAt) return undefined
  const startedAt = Number(scanStartedAt)
  if (!Number.isFinite(startedAt)) return undefined
  return { scanConfigId, scanJobId, startedAt }
}

/** live-loop's typed metric artifact, decoded from the flat artifact record. */
export function scenarioMetricArtifact(state: ScenarioState): ScenarioMetricArtifact | undefined {
  const artifacts = state.chapters['live-loop']?.artifacts
  if (!artifacts) return undefined
  const { metricId, metricStartedAt } = artifacts
  if (!metricId || !metricStartedAt) return undefined
  const startedAt = Number(metricStartedAt)
  if (!Number.isFinite(startedAt)) return undefined
  return { metricId, startedAt }
}

function encodeScanArtifact(artifact: ScenarioScanArtifact): Record<string, string> {
  return {
    scanConfigId: artifact.scanConfigId,
    scanJobId: artifact.scanJobId,
    scanStartedAt: String(artifact.startedAt),
  }
}

/**
 * True while an artifact the user started is still being watched. The strip
 * pulses on this; it is never true just because the demo's own tick is busy.
 */
export function isScenarioWatching(state: ScenarioState): boolean {
  if (state.activeChapter !== 'live-loop') return false
  const chapter = state.chapters['live-loop']
  if (!chapter || chapter.status !== 'active') return false
  if (chapter.step === 'live-loop/watch-scan') return scenarioScanArtifact(state) !== undefined
  if (chapter.step === 'live-loop/collect-metric') {
    return scenarioMetricArtifact(state) !== undefined
  }
  return false
}

function withChapter(
  state: ScenarioState,
  chapterId: ChapterId,
  chapter: ChapterState,
): ScenarioState {
  return { ...state, chapters: { ...state.chapters, [chapterId]: chapter } }
}

/**
 * Every transition. Immutable: each case returns a new state.
 *
 * Chapter lifecycle events act on any chapter; progress events act only on the
 * ACTIVE chapter while it is running, so a background poll resolving after the
 * user walked away (or switched chapters) cannot drag the strip back. live-loop
 * failures regress to a retryable step with a hint, exactly as in v1.
 */
export function scenarioReducer(state: ScenarioState, event: ScenarioEvent): ScenarioState {
  switch (event.type) {
    case 'startChapter': {
      const existing = state.chapters[event.chapter]
      // Resume an in-flight or dismissed chapter where it left off; a
      // completed (or never-started) one starts from its first step.
      const chapter =
        existing && (existing.status === 'active' || existing.status === 'dismissed')
          ? { ...existing, status: 'active' as const }
          : freshChapterState(event.chapter)
      const chapters = { ...state.chapters, [event.chapter]: chapter }
      // Pause the chapter being switched away from (progress kept), so only
      // one chapter ever holds status 'active' — the picker keys on it.
      const outgoingId = state.activeChapter
      if (outgoingId && outgoingId !== event.chapter) {
        const outgoing = state.chapters[outgoingId]
        if (outgoing?.status === 'active') {
          chapters[outgoingId] = { ...outgoing, status: 'dismissed' }
        }
      }
      return { v: 3, activeChapter: event.chapter, chapters, engaged: true }
    }

    case 'restartChapter':
      return {
        v: 3,
        activeChapter: event.chapter,
        chapters: { ...state.chapters, [event.chapter]: freshChapterState(event.chapter) },
        engaged: true,
      }

    case 'dismissChapter': {
      const existing = state.chapters[event.chapter]
      const activeChapter = state.activeChapter === event.chapter ? null : state.activeChapter
      // A completed chapter stays completed: dismissing its victory strip only
      // clears the pointer, so the picker and nextChapterId keep seeing it done.
      if (!existing || existing.status === 'dismissed' || existing.status === 'completed') {
        return activeChapter === state.activeChapter ? state : { ...state, activeChapter }
      }
      return {
        ...state,
        activeChapter,
        chapters: { ...state.chapters, [event.chapter]: { ...existing, status: 'dismissed' } },
      }
    }

    default:
      break
  }

  const chapterId = state.activeChapter
  if (!chapterId) return state
  const chapter = state.chapters[chapterId]
  if (!chapter || chapter.status !== 'active') return state

  if (event.type === 'stepCompleted') {
    if (stepChapter(event.step) !== chapterId || chapter.step !== event.step) return state
    const steps = CHAPTER_STEP_IDS[chapterId]
    const next = steps[steps.indexOf(event.step) + 1]
    // The editor the user actually opened is where the remaining edit-event
    // controls live — remember it so set-value/save deep-link back inside.
    const artifacts =
      event.step === 'edit-event/open-editor' && event.path
        ? { ...chapter.artifacts, editorPath: event.path }
        : chapter.artifacts
    const updated: ChapterState = next
      ? { ...chapter, step: next, artifacts, hint: undefined }
      : { ...chapter, status: 'completed', artifacts, hint: undefined }
    return withChapter(state, chapterId, updated)
  }

  // Everything below is live-loop's artifact machinery.
  if (chapterId !== 'live-loop') return state

  switch (event.type) {
    case 'scanRunStarted':
      // A second run before the first lands replaces the artifact: the user is
      // watching the run they just fired, not the one they abandoned.
      return withChapter(state, 'live-loop', {
        status: 'active',
        step: 'live-loop/watch-scan',
        artifacts: encodeScanArtifact({
          scanConfigId: event.scanConfigId,
          scanJobId: event.scanJobId,
          startedAt: event.at,
        }),
      })

    case 'scanSettled': {
      if (chapter.step !== 'live-loop/watch-scan' || !scenarioScanArtifact(state)) return state
      if (event.outcome === 'completed') {
        // The scan artifact survives the step: it is the record of what landed,
        // and the strip links back to it while the user collects a metric.
        return withChapter(state, 'live-loop', {
          status: 'active',
          step: 'live-loop/collect-metric',
          artifacts: chapter.artifacts,
        })
      }
      return withChapter(state, 'live-loop', {
        status: 'active',
        step: 'live-loop/run-scan',
        hint: event.outcome === 'stale' ? 'scan-stale' : 'scan-failed',
      })
    }

    case 'collectStarted': {
      if (chapter.step !== 'live-loop/collect-metric') return state
      return withChapter(state, 'live-loop', {
        ...chapter,
        artifacts: {
          ...chapter.artifacts,
          metricId: event.metricId,
          metricStartedAt: String(event.at),
        },
        hint: undefined,
      })
    }

    case 'collectSettled': {
      if (chapter.step !== 'live-loop/collect-metric' || !scenarioMetricArtifact(state)) {
        return state
      }
      if (event.outcome === 'success') {
        return withChapter(state, 'live-loop', {
          status: 'active',
          step: 'live-loop/see-chart',
          artifacts: chapter.artifacts,
        })
      }
      const scan = scenarioScanArtifact(state)
      return withChapter(state, 'live-loop', {
        status: 'active',
        step: 'live-loop/collect-metric',
        ...(scan ? { artifacts: encodeScanArtifact(scan) } : {}),
        hint: 'collect-failed',
      })
    }

    case 'chartVisited': {
      // Only the chart of the metric they actually collected finishes the
      // chapter; wandering onto some other metric's chart is not the payoff.
      if (chapter.step !== 'live-loop/see-chart') return state
      if (scenarioMetricArtifact(state)?.metricId !== event.metricId) return state
      return withChapter(state, 'live-loop', { ...chapter, status: 'completed', hint: undefined })
    }

    case 'watchTimedOut': {
      // The run may well still be going — we simply stop claiming to be
      // watching it, and hand the user back the action they can repeat.
      if (chapter.step === 'live-loop/watch-scan' && scenarioScanArtifact(state)) {
        return withChapter(state, 'live-loop', {
          status: 'active',
          step: 'live-loop/run-scan',
          hint: 'watch-timeout',
        })
      }
      if (chapter.step === 'live-loop/collect-metric' && scenarioMetricArtifact(state)) {
        const scan = scenarioScanArtifact(state)
        return withChapter(state, 'live-loop', {
          status: 'active',
          step: 'live-loop/collect-metric',
          ...(scan ? { artifacts: encodeScanArtifact(scan) } : {}),
          hint: 'watch-timeout',
        })
      }
      return state
    }
  }

  return state
}

/**
 * How a step's coach mark sits on its anchor. Owned by the model so every
 * surface that anchors the same step agrees on placement, and pages stop
 * hardcoding side/align at each call site.
 */
export interface ScenarioCoachPlacement {
  side: 'top' | 'right' | 'bottom' | 'left'
  align: 'start' | 'center' | 'end'
  /** 'ring' draws the pulsing beacon around the anchor; 'none' keeps only the card. */
  emphasis: 'ring' | 'none'
}

export interface ScenarioStep {
  id: ScenarioStepId
  title: string
  /** What to do next, in one line. Shown in the strip and in the coach mark. */
  instruction: string
  /** Where the action lives. Deep links follow the persisted artifacts. */
  to: string
  ctaLabel: string
  /** Absent on deep-link / visit steps that have no on-surface anchor at all. */
  coach?: ScenarioCoachPlacement
}

export interface ScenarioChapter {
  id: ChapterId
  title: string
  /** One line of what this chapter demonstrates — the picker's subtitle. */
  blurb: string
  steps: ScenarioStep[]
}

export const CHAPTER_TITLES: Record<ChapterId, string> = {
  'live-loop': 'Run the live loop',
  'edit-event': 'Edit an event',
  variables: 'Properties & value drift',
  branches: 'Review a branch',
  reconcile: 'Reconcile the plan',
  alerting: 'Route an alert',
  explore: 'Explore the rest',
}

export const CHAPTER_BLURBS: Record<ChapterId, string> = {
  'live-loop': 'Run a scan, watch it land, collect a metric, see the chart move.',
  'edit-event': 'Open a planned event, set a documented field value, save it.',
  variables: 'Compare observed values against the documented list and review a drift.',
  branches: 'Open the seeded feature branch, read its diff, leave a comment.',
  reconcile: 'Accept a shadow event and resolve a schema drift.',
  alerting: 'Create a rule on the local demo sink and simulate a firing.',
  explore: 'Coverage, the anomaly drilldown, and search — the rest of the map.',
}

/** The names the demo seeder guarantees. Coach marks key on these to highlight
 *  exactly one deterministic example row per anchor (single-example rule). */
export const SCENARIO_SEEDED = {
  editedEventName: 'Trial Started',
  editedFieldName: 'product_id',
  editedFieldValue: 'prod_monthly',
  editedFieldToken: '${product_id}',
  anomalyEventName: 'Home Screen View',
  driftVariableName: 'product_id',
  branchName: 'feature/checkout-funnel',
  changedEventName: 'Buy Button Click',
  shadowCandidateName: 'app_heartbeat_v1',
  schemaDriftEventName: 'Purchase Completed',
  firingRuleName: 'Spike & drift watch (demo)',
} as const

/**
 * A chapter's steps, links resolved against the state collected so far (the
 * live-loop deep links follow the persisted artifacts, exactly as in v1).
 */
export function buildChapterSteps(
  slug: string,
  chapterId: ChapterId,
  state: ScenarioState,
): [ScenarioStep, ...ScenarioStep[]] {
  const base = projectPath(currentOrgSlug(), slug)
  const scans = `${base}/scans`
  switch (chapterId) {
    case 'live-loop': {
      const scan = scenarioScanArtifact(state)
      const metric = scenarioMetricArtifact(state)
      return [
        {
          id: 'live-loop/run-scan',
          title: 'Run a scan',
          instruction: 'Run a scan to pull fresh volume from the demo warehouse.',
          to: scans,
          ctaLabel: 'Open Scans',
          // Read only by the scan header (ScanConfigDetailView), where Run now is
          // right-aligned and the header's own causal note runs 620px out to its
          // left. `align: 'end'` opened the card back across that note and cut it
          // mid-word, hiding the one clause that qualifies this very button:
          // metric points are collected on the schedule, "not by Run now"
          // (tripl-pbzs). `align: 'start'` grows the card into the header's empty
          // right gutter instead. The scan LIST uses the same step from inside a
          // <td>, where the card docks and this placement is ignored.
          coach: { side: 'bottom', align: 'start', emphasis: 'ring' },
        },
        {
          id: 'live-loop/watch-scan',
          title: 'Watch it land',
          instruction: 'Your scan is running. Watch it complete and see what it changed.',
          to: scan ? `${scans}/${scan.scanConfigId}` : scans,
          ctaLabel: 'Open the run',
          coach: { side: 'top', align: 'start', emphasis: 'ring' },
        },
        {
          id: 'live-loop/collect-metric',
          title: 'Collect a metric',
          instruction: 'Pick a metric and collect it now — the same worker a real project uses.',
          to: `${base}/metrics`,
          ctaLabel: 'Open Metrics',
          coach: { side: 'bottom', align: 'end', emphasis: 'ring' },
        },
        {
          id: 'live-loop/see-chart',
          title: 'See the chart move',
          instruction: 'Open the metric to see the series your collection just recomputed.',
          to: metric ? getMetricMonitoringPath(slug, metric.metricId) : `${base}/metrics`,
          ctaLabel: 'Open the chart',
          coach: { side: 'top', align: 'start', emphasis: 'ring' },
        },
      ]
    }
    case 'edit-event': {
      // Once open-editor lands, the recorded editor pathname is the surface the
      // remaining steps' controls live on — deep-link there, not at the list.
      const editorPath = state.chapters['edit-event']?.artifacts?.editorPath
      const editor = editorPath ?? `${base}/events`
      const editorCta = editorPath ? 'Open the editor' : 'Open Events'
      return [
        {
          id: 'edit-event/open-editor',
          title: 'Open an event',
          instruction: `Open ${SCENARIO_SEEDED.editedEventName} in the events catalog to edit it.`,
          to: `${base}/events`,
          ctaLabel: 'Open Events',
          coach: { side: 'bottom', align: 'start', emphasis: 'ring' },
        },
        {
          id: 'edit-event/set-value',
          title: 'Enter a sample Product ID',
          instruction: `Replace the current Product ID value with ${SCENARIO_SEEDED.editedFieldValue}. The guide advances automatically — do not save yet.`,
          to: editor,
          ctaLabel: editorCta,
          coach: { side: 'right', align: 'center', emphasis: 'ring' },
        },
        {
          id: 'edit-event/set-token',
          title: 'Restore the property template',
          instruction: `Replace ${SCENARIO_SEEDED.editedFieldValue}: type $ in Product ID, choose ${SCENARIO_SEEDED.editedFieldToken}, then follow the guide to Save.`,
          to: editor,
          ctaLabel: editorCta,
          coach: { side: 'right', align: 'center', emphasis: 'ring' },
        },
        {
          id: 'edit-event/save',
          title: 'Save the event',
          instruction: 'Save the event — the tracking plan updates immediately.',
          to: editor,
          ctaLabel: editorCta,
          coach: { side: 'top', align: 'end', emphasis: 'ring' },
        },
      ]
    }
    case 'variables':
      return [
        {
          id: 'variables/open-variables',
          title: 'Open Properties',
          instruction: 'Open the Properties settings — the templating layer behind field values.',
          to: `${base}/variables`,
          ctaLabel: 'Open Properties',
        },
        {
          id: 'variables/inspect-values',
          title: 'Inspect product_id',
          instruction: `Open ${SCENARIO_SEEDED.driftVariableName} to compare observed values against the documented list.`,
          to: `${base}/variables`,
          ctaLabel: 'Open Properties',
          coach: { side: 'left', align: 'center', emphasis: 'ring' },
        },
        {
          id: 'variables/see-drift',
          title: 'Review the value drift',
          instruction:
            'A scan saw prod_weekly outside the documented values — review the drift row.',
          to: `${base}/variables`,
          ctaLabel: 'Open Properties',
          coach: { side: 'bottom', align: 'end', emphasis: 'ring' },
        },
      ]
    case 'branches':
      return [
        {
          id: 'branches/open-branches',
          title: 'Open Branches',
          instruction: 'Open plan branches — version control for the tracking plan.',
          to: `${base}/branches`,
          ctaLabel: 'Open Branches',
        },
        {
          id: 'branches/open-branch',
          title: 'Open the feature branch',
          instruction: `Open ${SCENARIO_SEEDED.branchName} to review its pending change.`,
          to: `${base}/branches`,
          ctaLabel: 'Open Branches',
          coach: { side: 'right', align: 'center', emphasis: 'ring' },
        },
        {
          id: 'branches/review-diff',
          title: 'Review the diff',
          instruction: `Expand the change to ${SCENARIO_SEEDED.changedEventName} — one modified event, before and after.`,
          to: `${base}/branches`,
          ctaLabel: 'Open Branches',
          coach: { side: 'bottom', align: 'start', emphasis: 'ring' },
        },
        {
          id: 'branches/comment',
          title: 'Leave a comment',
          instruction: 'Post a review comment (or approve) — merging stays your call.',
          to: `${base}/branches`,
          ctaLabel: 'Open Branches',
          coach: { side: 'top', align: 'end', emphasis: 'ring' },
        },
      ]
    case 'reconcile':
      return [
        {
          id: 'reconcile/open-reconciliation',
          title: 'Open Reconciliation',
          instruction: 'Open Reconciliation — what the warehouse sees vs what the plan says.',
          to: `${base}/reconciliation`,
          ctaLabel: 'Open Reconciliation',
        },
        {
          id: 'reconcile/accept-shadow',
          title: 'Accept a shadow event',
          instruction: `Accept ${SCENARIO_SEEDED.shadowCandidateName} — warehouse traffic with no planned event.`,
          to: `${base}/reconciliation`,
          ctaLabel: 'Open Reconciliation',
          coach: { side: 'bottom', align: 'end', emphasis: 'ring' },
        },
        {
          id: 'reconcile/review-drift',
          title: 'Resolve a schema drift',
          instruction: `Open the schema-drift badge beside the Events heading and accept the amount type change on ${SCENARIO_SEEDED.schemaDriftEventName}.`,
          to: `${base}/events`,
          ctaLabel: 'Open Events',
          coach: { side: 'bottom', align: 'start', emphasis: 'ring' },
        },
      ]
    case 'alerting':
      return [
        {
          id: 'alerting/open-alerting',
          title: 'Open Alerting',
          instruction: 'Open alerting — destinations, rules and the local demo sink.',
          to: `${base}/alerting`,
          ctaLabel: 'Open Alerting',
        },
        {
          id: 'alerting/create-rule',
          title: 'Create a rule',
          instruction:
            'Add a rule on the local demo sink — deliveries render locally, nothing is sent.',
          to: `${base}/alerting?section=monitors`,
          ctaLabel: 'Open Rules',
          coach: { side: 'left', align: 'center', emphasis: 'ring' },
        },
        {
          id: 'alerting/simulate',
          title: 'Simulate a firing',
          instruction: `Replay ${SCENARIO_SEEDED.firingRuleName} to preview a delivery over real anomalies.`,
          to: `${base}/alerting?section=monitors`,
          ctaLabel: 'Open Rules',
          coach: { side: 'left', align: 'center', emphasis: 'ring' },
        },
      ]
    case 'explore':
      return [
        {
          id: 'explore/visit-coverage',
          title: 'Check coverage',
          instruction: 'Open Coverage — which platforms and event types actually report.',
          to: `${base}/coverage`,
          ctaLabel: 'Open Coverage',
        },
        {
          id: 'explore/visit-anomaly',
          title: 'Drill into the spike',
          instruction: `Open Anomalies and drill into the ${SCENARIO_SEEDED.anomalyEventName} spike.`,
          to: `${base}/anomalies`,
          ctaLabel: 'Open Anomalies',
        },
        {
          id: 'explore/use-search',
          title: 'Search by meaning',
          instruction:
            'Press Ctrl K (or ⌘K) and try "purchase funnel" or "money back" — search matches meaning, not just names.',
          to: `${base}/overview`,
          ctaLabel: 'Open Overview',
        },
      ]
  }
}

/** One row of the chapter picker: status plus the first step's deep link. */
export interface ChapterListEntry {
  id: ChapterId
  title: string
  blurb: string
  status: ChapterStatus
  to: string
}

/** Every chapter in order, resolved for the picker (tour / welcome / strip). */
export function buildChapterList(slug: string, state: ScenarioState): ChapterListEntry[] {
  return CHAPTER_IDS.map((chapterId) => ({
    id: chapterId,
    title: CHAPTER_TITLES[chapterId],
    blurb: CHAPTER_BLURBS[chapterId],
    status: chapterStatus(state, chapterId),
    to: buildChapterSteps(slug, chapterId, state)[0].to,
  }))
}

export function buildChapter(
  slug: string,
  chapterId: ChapterId,
  state: ScenarioState,
): ScenarioChapter {
  return {
    id: chapterId,
    title: CHAPTER_TITLES[chapterId],
    blurb: CHAPTER_BLURBS[chapterId],
    steps: buildChapterSteps(slug, chapterId, state),
  }
}

/** The active chapter's current step (first step of live-loop as a fallback,
 *  so consumers never hold a null step — mirrors v1's activeScenarioStep). */
export function activeScenarioStep(slug: string, state: ScenarioState): ScenarioStep {
  const chapterId = state.activeChapter ?? 'live-loop'
  const steps = buildChapterSteps(slug, chapterId, state)
  const stepId = state.chapters[chapterId]?.step
  return steps.find((step) => step.id === stepId) ?? steps[0]
}

/**
 * Deep-link and explore steps complete by ARRIVING somewhere, mirroring how
 * see-chart completes on a route visit. One pure predicate so the provider's
 * route watcher and the tests agree on what counts as arrival.
 */
export function stepCompletedByPath(
  slug: string,
  step: ScenarioStepId,
  pathname: string,
): boolean {
  const base = projectPath(currentOrgSlug(), slug)
  switch (step) {
    case 'edit-event/open-editor':
      // /p/:slug/events/:tab/:eventId/edit — the editor for ANY event counts;
      // the coach mark steers towards the seeded one, but the model cannot
      // know event ids, and editing anything still teaches the surface.
      return pathname.startsWith(`${base}/events/`) && pathname.endsWith('/edit')
    case 'variables/open-variables':
      return pathname.startsWith(`${base}/variables`)
    case 'branches/open-branches':
      return pathname.startsWith(`${base}/branches`)
    case 'reconcile/open-reconciliation':
      return pathname.startsWith(`${base}/reconciliation`)
    case 'alerting/open-alerting':
      return pathname.startsWith(`${base}/alerting`)
    case 'explore/visit-coverage':
      return pathname.startsWith(`${base}/coverage`)
    case 'explore/visit-anomaly':
      // The anomalies list links into /monitoring/:scope/:id — the drilldown
      // is the destination that completes the step.
      return pathname.startsWith(`${base}/monitoring/`)
    default:
      return false
  }
}

const STORAGE_PREFIX = SCENARIO_STORAGE_PREFIX

/** The localStorage key a slug's progress lives under — for cross-tab sync. */
export function scenarioStorageKey(slug: string): string {
  return `${STORAGE_PREFIX}${slug}`
}

function isScanArtifact(value: unknown): value is ScenarioScanArtifact {
  if (typeof value !== 'object' || value === null) return false
  const artifact = value as Record<string, unknown>
  return (
    typeof artifact.scanConfigId === 'string' &&
    typeof artifact.scanJobId === 'string' &&
    typeof artifact.startedAt === 'number'
  )
}

function isMetricArtifact(value: unknown): value is ScenarioMetricArtifact {
  if (typeof value !== 'object' || value === null) return false
  const artifact = value as Record<string, unknown>
  return typeof artifact.metricId === 'string' && typeof artifact.startedAt === 'number'
}

const V1_STEP_TO_V2: Record<string, ScenarioStepId> = {
  'run-scan': 'live-loop/run-scan',
  'watch-scan': 'live-loop/watch-scan',
  'collect-metric': 'live-loop/collect-metric',
  'see-chart': 'live-loop/see-chart',
}

/** A valid v1 blob becomes chapters['live-loop']; anything half-trusted reads
 *  fresh — a step whose artifact failed validation could never complete. */
function migrateV1(candidate: Record<string, unknown>): ScenarioState | null {
  const status = candidate.status
  if (status !== 'active' && status !== 'completed' && status !== 'dismissed') return null
  const step = V1_STEP_TO_V2[candidate.step as string]
  if (!step) return null
  if (candidate.scan !== undefined && !isScanArtifact(candidate.scan)) return null
  if (candidate.metric !== undefined && !isMetricArtifact(candidate.metric)) return null
  if (candidate.hint !== undefined && !SCENARIO_HINTS.includes(candidate.hint as ScenarioHint)) {
    return null
  }

  const artifacts: Record<string, string> = {}
  if (candidate.scan) {
    Object.assign(artifacts, encodeScanArtifact(candidate.scan as ScenarioScanArtifact))
  }
  if (candidate.metric) {
    const metric = candidate.metric as ScenarioMetricArtifact
    artifacts.metricId = metric.metricId
    artifacts.metricStartedAt = String(metric.startedAt)
  }

  return {
    v: 3,
    // A dismissed v1 run stays put away; anything else keeps coaching.
    activeChapter: status === 'dismissed' ? null : 'live-loop',
    chapters: {
      'live-loop': {
        status,
        step,
        ...(Object.keys(artifacts).length > 0 ? { artifacts } : {}),
        ...(candidate.hint ? { hint: candidate.hint as ScenarioHint } : {}),
      },
    },
  }
}

function isChapterState(chapterId: ChapterId, value: unknown): value is ChapterState {
  if (typeof value !== 'object' || value === null) return false
  const candidate = value as Record<string, unknown>
  if (!CHAPTER_STATUSES.includes(candidate.status as ChapterStatus)) return false
  if (!CHAPTER_STEP_IDS[chapterId].includes(candidate.step as ScenarioStepId)) return false
  if (candidate.artifacts !== undefined) {
    if (typeof candidate.artifacts !== 'object' || candidate.artifacts === null) return false
    if (Object.values(candidate.artifacts).some((entry) => typeof entry !== 'string')) {
      return false
    }
  }
  if (candidate.hint !== undefined && !SCENARIO_HINTS.includes(candidate.hint as ScenarioHint)) {
    return false
  }
  return true
}

/**
 * Anything we cannot fully vouch for reads as a fresh scenario. A half-trusted
 * state is worse than none: a step whose artifact failed validation can never
 * complete, so the user would be coached towards a dead end.
 */
function parseStoredChapters(
  candidate: Record<string, unknown>,
): Omit<ScenarioState, 'v'> | null {
  const activeChapter = candidate.activeChapter
  if (activeChapter !== null && !CHAPTER_IDS.includes(activeChapter as ChapterId)) return null
  if (typeof candidate.chapters !== 'object' || candidate.chapters === null) return null

  const chapters: Partial<Record<ChapterId, ChapterState>> = {}
  for (const [key, value] of Object.entries(candidate.chapters)) {
    if (!CHAPTER_IDS.includes(key as ChapterId)) return null
    if (!isChapterState(key as ChapterId, value)) return null
    chapters[key as ChapterId] = value
  }
  // An active pointer at a chapter with no state would coach towards nothing.
  if (activeChapter !== null && !chapters[activeChapter as ChapterId]) return null

  return { activeChapter: activeChapter as ChapterId | null, chapters }
}

function migrateV2(candidate: Record<string, unknown>): ScenarioState | null {
  const stored = parseStoredChapters(candidate)
  if (!stored) return null

  const legacyEdit = stored.chapters['edit-event']
  if (!legacyEdit) return { v: 3, ...stored }

  const chapters = { ...stored.chapters }
  if (stored.activeChapter === 'edit-event' && legacyEdit.status === 'active') {
    chapters['edit-event'] = freshChapterState('edit-event')
    return { v: 3, activeChapter: 'edit-event', chapters }
  } else {
    delete chapters['edit-event']
  }
  const activeChapter = stored.activeChapter === 'edit-event' ? null : stored.activeChapter
  return { v: 3, activeChapter, chapters }
}

function parseScenarioState(raw: string): ScenarioState | null {
  const parsed: unknown = JSON.parse(raw)
  if (typeof parsed !== 'object' || parsed === null) return null
  const candidate = parsed as Record<string, unknown>

  if (candidate.v === 1) return migrateV1(candidate)
  if (candidate.v === 2) return migrateV2(candidate)
  if (candidate.v !== 3) return null

  const stored = parseStoredChapters(candidate)
  if (!stored) return null
  return candidate.engaged === true ? { v: 3, ...stored, engaged: true } : { v: 3, ...stored }
}

export function readScenarioState(slug: string): ScenarioState {
  if (typeof window === 'undefined') return initialScenarioState()
  try {
    const raw = window.localStorage.getItem(orgStorageKey(`${STORAGE_PREFIX}${slug}`))
    if (raw === null) return initialScenarioState()
    return parseScenarioState(raw) ?? initialScenarioState()
  } catch {
    return initialScenarioState()
  }
}

export function writeScenarioState(slug: string, state: ScenarioState): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(orgStorageKey(`${STORAGE_PREFIX}${slug}`), JSON.stringify(state))
  } catch {
    /* ignore — a scenario that cannot remember its place still coaches this session */
  }
}

/** Every step id, exported for exhaustive checks in tests. */
export const SCENARIO_STEP_IDS: readonly ScenarioStepId[] = ALL_STEP_IDS
