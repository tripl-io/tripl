/**
 * The stateful engine of the coached demo scenario (chapters in
 * tripl-odrj.4).
 *
 * Runs once, under the app shell (LazyDemoScenarioProvider in Layout), so the
 * scenario survives every navigation the chains require: the scan the user started keeps being watched while they walk to
 * the metrics catalog, and a reload mid-run picks the watch back up from the
 * persisted job id.
 *
 * Completion is deliberately split by chapter:
 *
 * - live-loop keeps its own SILENT polls. The SSE stream hands
 *   components no event payloads — only query invalidation — and a page's job
 *   query dies with the page, so the scenario polls the one job the user's own
 *   action produced, by id. Silent, because both metric surfaces already run
 *   a detached `startMetricCollectionWatch` and toast the outcome.
 * - every other chapter advances on DIRECT notifications: a route visit
 *   (`stepCompletedByPath`, checked here) or a `notifyStepCompleted` fired from
 *   the exact mutation the user performed. No polling for them.
 *
 * Nothing here trusts the demo's own runtime tick: it manufactures
 * real scan jobs and real collections continuously, so only the artifacts the
 * user's own action produced can move live-loop forward.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '@/api/client'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import { scansApi } from '@/api/scans'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { getMetricMonitoringPath } from '@/lib/monitoring'
import type { Project, ScanJob } from '@/types'
import {
  INERT_ACTIONS,
  INERT_SCENARIO,
  isCoachableDemo,
  type DemoScenarioActions,
  type DemoScenarioValue,
  type ScenarioArtifacts,
} from './demoScenarioContext'
import {
  activeChapterState,
  activeScenarioStep,
  buildChapterList,
  buildChapterSteps,
  initialScenarioState,
  isScenarioActive,
  isScenarioWatching,
  nextChapterId,
  readScenarioState,
  scenarioMetricArtifact,
  scenarioReducer,
  scenarioScanArtifact,
  scenarioStorageKey,
  stepCompletedByPath,
  writeScenarioState,
  type ScenarioEvent,
  type ScenarioState,
} from './scenarioModel'
import { readHintsMuted, writeHintsMuted } from './demoLocalState'
import {
  demoScenarioCollectWatchKey,
  demoScenarioScanWatchKey,
  scanJobsKey,
} from '@/lib/queryKeys'

/** How often to re-check the artifact the user is waiting on. */
const DEFAULT_POLL_INTERVAL_MS = 3000

/**
 * Stop claiming to watch after this long. The worker's budget is far larger, so
 * a long run is not an error — the scenario simply stops pretending it is still
 * following along and hands the user back an action they can repeat.
 */
const WATCH_TIMEOUT_MS = 5 * 60_000

/** `MetricDefinition.last_collection_status` markers stamped by the backend. */
const COLLECTION_RUNNING = 'running'
const COLLECTION_ERROR = 'error'

function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404
}

function syncWatchedJob(current: ScanJob[] | undefined, job: ScanJob): ScanJob[] {
  if (!current) return [job]
  const found = current.some(candidate => candidate.id === job.id)
  return found
    ? current.map(candidate => candidate.id === job.id ? job : candidate)
    : [job, ...current]
}

/** The ids the pages point coach marks at, for a running scenario only. */
function scenarioArtifacts(state: ScenarioState): ScenarioArtifacts {
  if (!isScenarioActive(state)) return { scanConfigId: null, scanJobId: null, metricId: null }
  const scan = scenarioScanArtifact(state)
  const metric = scenarioMetricArtifact(state)
  return {
    scanConfigId: scan?.scanConfigId ?? null,
    scanJobId: scan?.scanJobId ?? null,
    metricId: metric?.metricId ?? null,
  }
}

/**
 * Everything the scenario contexts carry, for the project in scope: inert
 * values for anything that is not a ready demo. The providers around it only
 * decide how the model is loaded (DemoScenarioProvider eagerly, for tests and
 * direct use; LazyDemoScenarioProvider on demand, in the app shell).
 */
export function useDemoScenarioRuntime(
  project: Project | undefined,
  pollIntervalMs?: number,
): { value: DemoScenarioValue; actions: DemoScenarioActions } {
  const slug = project?.slug
  const location = useLocation()
  const queryClient = useQueryClient()

  const isDemoReady = isCoachableDemo(project)

  const [state, setState] = useState<ScenarioState>(() =>
    slug ? readScenarioState(slug) : initialScenarioState(),
  )
  // "Hide hints" lasts for the browser session, per project, as the coach
  // mark promises — it used to be plain state, lost on a reload or a trip to
  // another project and back (DEMO-15).
  const [hintsMuted, setHintsMutedState] = useState(() => readHintsMuted(slug))
  const setHintsMuted = useCallback(
    (muted: boolean) => {
      setHintsMutedState(muted)
      writeHintsMuted(slug, muted)
    },
    [slug],
  )

  // Re-read when the route walks to another project. Adjusting state during
  // render (the pattern Layout already uses) rather than in an effect, so the
  // first paint of the new project never shows the previous one's step.
  // What was read, kept beside the slug it was read for: it is what storage
  // holds for that project, so the write below can tell an advance made in the
  // very commit that switched projects from the copy it loaded.
  const [loaded, setLoaded] = useState({ slug, state })
  if (loaded.slug !== slug) {
    const next = slug ? readScenarioState(slug) : initialScenarioState()
    setLoaded({ slug, state: next })
    setState(next)
    setHintsMutedState(readHintsMuted(slug))
  }

  // The updater stays pure: a render React throws away must not leave a
  // persisted advance behind (DEMO-14). The state is written once it is
  // committed — and only when it differs from what storage already holds: the
  // copy a project switch loaded, or one read from another tab. The switch
  // itself is not skipped wholesale: arriving on the new project's current
  // deep-link step advances it in that same commit, and that advance must
  // reach storage like any other.
  const persistedRef = useRef<{ slug: string | undefined; state: ScenarioState }>({ slug, state })
  useEffect(() => {
    const last = persistedRef.current
    persistedRef.current = { slug, state }
    if (!slug) return
    const stored = last.slug === slug ? last.state : loaded.state
    if (state === stored) return
    writeScenarioState(slug, state)
  }, [slug, state, loaded])

  // Another tab on the same demo writes the same key. Without listening, each
  // tab overwrote the other's progress with its own on every step, last writer
  // wins (DEMO-16). Adopting the other tab's copy is marked as persisted, so it
  // is not written straight back.
  useEffect(() => {
    if (!slug) return
    const key = scenarioStorageKey(slug)
    const onStorage = (event: StorageEvent) => {
      if (event.key !== key && event.key !== null) return
      const next = readScenarioState(slug)
      persistedRef.current = { slug, state: next }
      setState(next)
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [slug])

  const dispatch = useCallback((event: ScenarioEvent) => {
    setState((prev) => scenarioReducer(prev, event))
  }, [])

  const running = isDemoReady && isScenarioActive(state)
  const chapter = activeChapterState(state)
  const onLiveLoop = running && state.activeChapter === 'live-loop'
  const scanTarget =
    onLiveLoop && chapter?.step === 'live-loop/watch-scan'
      ? scenarioScanArtifact(state)
      : undefined
  const metricTarget =
    onLiveLoop && chapter?.step === 'live-loop/collect-metric'
      ? scenarioMetricArtifact(state)
      : undefined

  // Watch the job the user's own run created — by id, so the tick's own jobs
  // (and any job some other tab started) are invisible to the scenario.
  useQuery({
    queryKey: demoScenarioScanWatchKey(slug, scanTarget?.scanJobId),
    enabled: Boolean(slug && scanTarget),
    meta: SILENT_ERROR_META,
    refetchInterval: pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS,
    gcTime: 0,
    retry: false,
    queryFn: async () => {
      if (!slug || !scanTarget) return null
      try {
        const job = await scansApi.getJob(slug, scanTarget.scanConfigId, scanTarget.scanJobId)
        // The scan page renders a separate list query whose fallback polling is
        // intentionally disabled while realtime is live. Mirror this exact-job
        // response into that cache so a missed terminal SSE event cannot leave
        // the same tab showing Running after the coach has already advanced.
        const terminal =
          job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled'
        if (terminal) {
          const listKey = scanJobsKey(slug, scanTarget.scanConfigId)
          queryClient.setQueryData<ScanJob[]>(listKey, current => syncWatchedJob(current, job))
          // The Scans list caches a capped history under the same prefix
          // (`[...listKey, { limit }]`); its Recent runs row is the one the
          // watch-scan coach points at, so it gets the same answer.
          queryClient.setQueriesData<ScanJob[]>({ queryKey: listKey }, current =>
            current ? syncWatchedJob(current, job) : current,
          )
          // Populate an absent cache immediately, then refresh the complete list
          // from the now-committed backend state. This also cancels any older
          // in-flight snapshot that could otherwise land as Running afterwards.
          void queryClient.invalidateQueries({ queryKey: listKey })
        }
        if (job.status === 'completed') dispatch({ type: 'scanSettled', outcome: 'completed' })
        else if (job.status === 'failed') dispatch({ type: 'scanSettled', outcome: 'failed' })
        else if (job.status === 'cancelled') dispatch({ type: 'scanSettled', outcome: 'cancelled' })
        else if (Date.now() - scanTarget.startedAt >= WATCH_TIMEOUT_MS) {
          dispatch({ type: 'watchTimedOut' })
        }
        return job
      } catch (error) {
        // The job is gone: the demo was reset out from under the scenario.
        if (isNotFound(error)) {
          dispatch({ type: 'scanSettled', outcome: 'stale' })
          return null
        }
        throw error
      }
    },
  })

  // The collect has no job model of its own: `POST /metrics/{id}/collect` stamps
  // the definition's `last_collection_status`, and the worker settles it. So the
  // definition is the run status.
  useQuery({
    queryKey: demoScenarioCollectWatchKey(slug, metricTarget?.metricId, metricTarget?.startedAt),
    enabled: Boolean(slug && metricTarget),
    meta: SILENT_ERROR_META,
    refetchInterval: pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS,
    gcTime: 0,
    retry: false,
    queryFn: async () => {
      if (!slug || !metricTarget) return null
      try {
        const definition = await metricsCatalogApi.get(slug, metricTarget.metricId)
        const status = definition.last_collection_status
        if (status === COLLECTION_RUNNING || status === null) {
          if (Date.now() - metricTarget.startedAt >= WATCH_TIMEOUT_MS) {
            dispatch({ type: 'watchTimedOut' })
          }
        } else if (status === COLLECTION_ERROR) {
          dispatch({ type: 'collectSettled', outcome: 'error' })
        } else {
          dispatch({ type: 'collectSettled', outcome: 'success' })
        }
        return definition
      } catch (error) {
        // The metric was deleted mid-watch — treat it as a failed collect so the
        // user is sent back to pick another one rather than left waiting.
        if (isNotFound(error)) {
          dispatch({ type: 'collectSettled', outcome: 'error' })
          return null
        }
        throw error
      }
    },
  })

  // Landing on the chart of the metric they collected is the payoff, and seeing
  // it is the whole step — so visiting completes it, exactly as visiting a
  // surface advances the tour.
  //
  // Checked at render rather than on a route change: the collection can settle
  // while the user is *already standing on* the chart, and no navigation would
  // follow to notice. This is React's "adjust state while rendering" pattern
  // and is safe as such since the updater stopped writing storage (DEMO-14):
  // a render React throws away now leaves nothing behind, because the write
  // happens in the persist effect above, after a commit.
  const seeChartMetric =
    onLiveLoop && chapter?.step === 'live-loop/see-chart'
      ? scenarioMetricArtifact(state)
      : undefined
  const chartPath =
    slug && seeChartMetric ? getMetricMonitoringPath(slug, seeChartMetric.metricId) : null
  if (seeChartMetric && chartPath === location.pathname) {
    dispatch({ type: 'chartVisited', metricId: seeChartMetric.metricId })
  }

  // Deep-link and explore steps complete by ARRIVING somewhere. Same render-time
  // check as the chart above; the reducer advances at most one step per render,
  // and no two consecutive steps share an arrival path (scenarioModel.test.ts
  // holds every chapter to that).
  const currentStepId = running ? chapter?.step : undefined
  if (slug && currentStepId && stepCompletedByPath(slug, currentStepId, location.pathname)) {
    // The pathname rides along so edit-event can remember which editor the
    // user opened and deep-link its later steps back into it.
    dispatch({ type: 'stepCompleted', step: currentStepId, path: location.pathname })
  }

  const actions = useMemo<DemoScenarioActions>(
    () => ({
      notifyScanRunStarted: (job) =>
        dispatch({
          type: 'scanRunStarted',
          scanConfigId: job.scan_config_id,
          scanJobId: job.id,
          at: Date.now(),
        }),
      notifyMetricCollectStarted: (metricId) =>
        dispatch({ type: 'collectStarted', metricId, at: Date.now() }),
      notifyStepCompleted: (step) => dispatch({ type: 'stepCompleted', step }),
      startChapter: (chapterId) => dispatch({ type: 'startChapter', chapter: chapterId }),
      restartChapter: (chapterId) => {
        setHintsMuted(false)
        dispatch({ type: 'restartChapter', chapter: chapterId })
      },
      dismissChapter: (chapterId) => dispatch({ type: 'dismissChapter', chapter: chapterId }),
      muteHints: () => setHintsMuted(true),
      unmuteHints: () => setHintsMuted(false),
      // Not a reducer event: this discards the persisted blob outright rather
      // than transitioning it, which is exactly what a re-seed does to the data
      // the old progress was recorded against.
      resetScenario: () => {
        setHintsMuted(false)
        const fresh = initialScenarioState()
        setState(fresh)
        // Written here as well as by the persist effect: the caller (the
        // banner's reset) navigates in the same tick, and the fresh blob must
        // be in storage whatever happens to this render.
        if (slug) writeScenarioState(slug, fresh)
      },
    }),
    [dispatch, setHintsMuted, slug],
  )

  const value = useMemo<DemoScenarioValue>(() => {
    if (!isDemoReady || !slug) return INERT_SCENARIO
    const chapters = buildChapterList(slug, state)
    const nextId = nextChapterId(state)
    return {
      available: true,
      active: isScenarioActive(state),
      state,
      activeChapter: state.activeChapter,
      step: activeScenarioStep(slug, state),
      steps: buildChapterSteps(slug, state.activeChapter ?? 'live-loop', state),
      chapters,
      nextChapter: nextId ? (chapters.find((entry) => entry.id === nextId) ?? null) : null,
      isWatching: isScenarioWatching(state),
      hintsMuted,
      artifacts: scenarioArtifacts(state),
    }
  }, [isDemoReady, slug, state, hintsMuted])

  return { value, actions: isDemoReady ? actions : INERT_ACTIONS }
}
