// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import {
  CHAPTER_IDS,
  CHAPTER_STEP_IDS,
  SCENARIO_SEEDED,
  SCENARIO_STEP_IDS,
  activeScenarioStep,
  buildChapterList,
  buildChapterSteps,
  chapterStatus,
  initialScenarioState,
  isScenarioActive,
  isScenarioWatching,
  nextChapterId,
  readScenarioState,
  scenarioMetricArtifact,
  scenarioReducer,
  scenarioScanArtifact,
  scenarioStepIndex,
  stepChapter,
  stepCompletedByPath,
  writeScenarioState,
  type ScenarioState,
} from './scenarioModel'
import { at } from '@/test/at'

const SLUG = 'acme'
const KEY = `tripl-demo-scenario:${SLUG}`

/** State as it looks the moment the user's run is accepted. */
function watching(): ScenarioState {
  return scenarioReducer(initialScenarioState(), {
    type: 'scanRunStarted',
    scanConfigId: 'sc-1',
    scanJobId: 'job-1',
    at: 1000,
  })
}

/** State after the run landed and a collect is in flight. */
function collecting(): ScenarioState {
  const landed = scenarioReducer(watching(), { type: 'scanSettled', outcome: 'completed' })
  return scenarioReducer(landed, { type: 'collectStarted', metricId: 'm-1', at: 2000 })
}

function liveLoop(state: ScenarioState) {
  return state.chapters['live-loop']
}

afterEach(() => {
  window.localStorage.clear()
})

describe('the chapter/step id contract', () => {
  it('scopes every step id to its chapter', () => {
    for (const chapter of CHAPTER_IDS) {
      for (const step of CHAPTER_STEP_IDS[chapter]) {
        expect(step.startsWith(`${chapter}/`)).toBe(true)
        expect(stepChapter(step)).toBe(chapter)
      }
    }
  })

  it('exports one flat step list covering every chapter', () => {
    expect(SCENARIO_STEP_IDS).toHaveLength(
      CHAPTER_IDS.reduce((total, chapter) => total + CHAPTER_STEP_IDS[chapter].length, 0),
    )
  })
})

describe('scenario chapter browser contracts', () => {
  it('places the chart coach above its row so it does not cover the catalog below', () => {
    const seeChart = buildChapterSteps(SLUG, 'live-loop', initialScenarioState()).find(
      step => step.id === 'live-loop/see-chart',
    )

    expect(seeChart?.coach).toEqual({ side: 'top', align: 'start', emphasis: 'ring' })
  })

  it('opens the run-scan coach away from the header note it would otherwise cover', () => {
    // The scan header's Run now is right-aligned with the causal note — "metric
    // points … are collected on that schedule, not by Run now" — running 620px
    // out to its left. `align: 'end'` opened the card back across that note and
    // cut it mid-word, hiding the one clause that qualifies the button the card
    // is pointing at.
    const runScan = buildChapterSteps(SLUG, 'live-loop', initialScenarioState()).find(
      step => step.id === 'live-loop/run-scan',
    )

    expect(runScan?.coach).toEqual({ side: 'bottom', align: 'start', emphasis: 'ring' })
  })

  it.each([
    ['variables', 'variables/see-drift', { side: 'bottom', align: 'end', emphasis: 'ring' }],
    ['branches', 'branches/review-diff', { side: 'bottom', align: 'start', emphasis: 'ring' }],
    ['reconcile', 'reconcile/accept-shadow', { side: 'bottom', align: 'end', emphasis: 'ring' }],
  ] as const)('keeps the %s action coach clear of nearby content', (chapter, stepId, placement) => {
    const step = buildChapterSteps(SLUG, chapter, initialScenarioState()).find(
      candidate => candidate.id === stepId,
    )

    expect(step?.coach).toEqual(placement)
  })

  it('edits the seeded product field to a documented value without changing the anomaly target', () => {
    const editSteps = buildChapterSteps(SLUG, 'edit-event', initialScenarioState())
    const exploreSteps = buildChapterSteps(SLUG, 'explore', initialScenarioState())

    expect(SCENARIO_SEEDED.editedEventName).toBe('Trial Started')
    expect(SCENARIO_SEEDED.editedFieldName).toBe('product_id')
    expect(SCENARIO_SEEDED.editedFieldValue).toBe('prod_monthly')
    expect(SCENARIO_SEEDED.editedFieldToken).toBe('${product_id}')
    expect(SCENARIO_SEEDED.anomalyEventName).toBe('Home Screen View')
    expect(editSteps[0].instruction).toContain('Trial Started')
    expect(editSteps[1]).toMatchObject({
      title: 'Enter a sample Product ID',
      instruction:
        'Replace the current Product ID value with prod_monthly. The guide advances automatically — do not save yet.',
    })
    expect(editSteps[2]).toMatchObject({
      title: 'Restore the property template',
      instruction:
        'Replace prod_monthly: type $ in Product ID, choose ${product_id}, then follow the guide to Save.',
    })
    expect(at(exploreSteps, 1).instruction).toContain('Home Screen View')
  })
})

describe('scenarioReducer — live-loop, the happy chain', () => {
  it('runs scan -> watch -> collect -> chart -> completed', () => {
    const run = watching()
    expect(liveLoop(run)?.step).toBe('live-loop/watch-scan')
    expect(scenarioScanArtifact(run)).toEqual({
      scanConfigId: 'sc-1',
      scanJobId: 'job-1',
      startedAt: 1000,
    })

    const landed = scenarioReducer(run, { type: 'scanSettled', outcome: 'completed' })
    expect(liveLoop(landed)?.step).toBe('live-loop/collect-metric')
    // The landed run is kept — the strip links back to what it changed.
    expect(scenarioScanArtifact(landed)?.scanJobId).toBe('job-1')

    const collect = scenarioReducer(landed, { type: 'collectStarted', metricId: 'm-1', at: 2000 })
    expect(liveLoop(collect)?.step).toBe('live-loop/collect-metric')
    expect(scenarioMetricArtifact(collect)).toEqual({ metricId: 'm-1', startedAt: 2000 })

    const collected = scenarioReducer(collect, { type: 'collectSettled', outcome: 'success' })
    expect(liveLoop(collected)?.step).toBe('live-loop/see-chart')

    const done = scenarioReducer(collected, { type: 'chartVisited', metricId: 'm-1' })
    expect(liveLoop(done)?.status).toBe('completed')
    expect(chapterStatus(done, 'live-loop')).toBe('completed')
  })

  it('does not mutate the state it is given', () => {
    const before = watching()
    const snapshot = structuredClone(before)
    scenarioReducer(before, { type: 'scanSettled', outcome: 'completed' })
    expect(before).toEqual(snapshot)
  })
})

describe('scenarioReducer — live-loop regressions', () => {
  it('sends a failed run back to run-scan with a hint and no dead artifact', () => {
    const state = scenarioReducer(watching(), { type: 'scanSettled', outcome: 'failed' })
    expect(liveLoop(state)).toEqual({
      status: 'active',
      step: 'live-loop/run-scan',
      hint: 'scan-failed',
    })
  })

  it('reports a job that vanished under a demo reset as stale', () => {
    const state = scenarioReducer(watching(), { type: 'scanSettled', outcome: 'stale' })
    expect(liveLoop(state)?.hint).toBe('scan-stale')
    expect(scenarioScanArtifact(state)).toBeUndefined()
  })

  it('keeps a failed collect on collect-metric and drops the metric', () => {
    const state = scenarioReducer(collecting(), { type: 'collectSettled', outcome: 'error' })
    expect(liveLoop(state)?.step).toBe('live-loop/collect-metric')
    expect(liveLoop(state)?.hint).toBe('collect-failed')
    expect(scenarioMetricArtifact(state)).toBeUndefined()
  })

  it('hands a timed-out scan watch back to run-scan', () => {
    const state = scenarioReducer(watching(), { type: 'watchTimedOut' })
    expect(liveLoop(state)?.step).toBe('live-loop/run-scan')
    expect(liveLoop(state)?.hint).toBe('watch-timeout')
  })

  it('ignores a timeout when nothing is being watched', () => {
    const idle = initialScenarioState()
    expect(scenarioReducer(idle, { type: 'watchTimedOut' })).toEqual(idle)
  })

  it('ignores live-loop machinery while another chapter is active', () => {
    const branches = scenarioReducer(watching(), { type: 'startChapter', chapter: 'branches' })
    const late = scenarioReducer(branches, { type: 'scanSettled', outcome: 'completed' })
    // The poll resolving after a chapter switch must not touch live-loop.
    expect(late).toEqual(branches)
  })
})

describe('scenarioReducer — stepCompleted', () => {
  it('advances the active chapter one step at a time and completes at the end', () => {
    let state = scenarioReducer(initialScenarioState(), {
      type: 'startChapter',
      chapter: 'edit-event',
    })
    state = scenarioReducer(state, { type: 'stepCompleted', step: 'edit-event/open-editor' })
    expect(state.chapters['edit-event']?.step).toBe('edit-event/set-value')

    state = scenarioReducer(state, { type: 'stepCompleted', step: 'edit-event/set-value' })
    expect(state.chapters['edit-event']?.step).toBe('edit-event/set-token')

    state = scenarioReducer(state, { type: 'stepCompleted', step: 'edit-event/set-token' })
    expect(state.chapters['edit-event']?.step).toBe('edit-event/save')

    state = scenarioReducer(state, { type: 'stepCompleted', step: 'edit-event/save' })
    expect(state.chapters['edit-event']?.status).toBe('completed')
    expect(isScenarioActive(state)).toBe(false)
  })

  it('remembers the visited editor path and deep-links later edit-event steps into it', () => {
    const editorPath = `/p/${SLUG}/events/all/ev-1/edit`
    let state = scenarioReducer(initialScenarioState(), {
      type: 'startChapter',
      chapter: 'edit-event',
    })
    state = scenarioReducer(state, {
      type: 'stepCompleted',
      step: 'edit-event/open-editor',
      path: editorPath,
    })
    expect(state.chapters['edit-event']?.artifacts?.editorPath).toBe(editorPath)

    // set-value and save lead back into the editor the user actually opened,
    // not at the events list — a surface that cannot host their controls.
    const steps = buildChapterSteps(SLUG, 'edit-event', state)
    expect(at(steps, 1).to).toBe(editorPath)
    expect(at(steps, 2).to).toBe(editorPath)
  })

  it('falls back to the events list while no editor has been visited', () => {
    const steps = buildChapterSteps(SLUG, 'edit-event', initialScenarioState())
    expect(at(steps, 1).to).toBe(`/p/${SLUG}/events`)
    expect(at(steps, 2).to).toBe(`/p/${SLUG}/events`)
  })

  it('drops a notify for a step that is not the current one, so nothing skips ahead', () => {
    const state = scenarioReducer(initialScenarioState(), {
      type: 'startChapter',
      chapter: 'edit-event',
    })
    const stray = scenarioReducer(state, { type: 'stepCompleted', step: 'edit-event/save' })
    expect(stray).toEqual(state)
  })

  it("drops a notify for another chapter's step entirely", () => {
    const state = scenarioReducer(initialScenarioState(), {
      type: 'startChapter',
      chapter: 'variables',
    })
    const stray = scenarioReducer(state, { type: 'stepCompleted', step: 'branches/comment' })
    expect(stray).toEqual(state)
  })

  it('ignores every progress event while no chapter is active', () => {
    const dismissed = scenarioReducer(initialScenarioState(), {
      type: 'dismissChapter',
      chapter: 'live-loop',
    })
    expect(
      scenarioReducer(dismissed, { type: 'stepCompleted', step: 'live-loop/run-scan' }),
    ).toEqual(dismissed)
  })
})

describe('scenarioReducer — chapter lifecycle', () => {
  it('startChapter opens a fresh chapter on its first step', () => {
    const state = scenarioReducer(watching(), {
      type: 'startChapter',
      chapter: 'branches',
    })
    expect(state.activeChapter).toBe('branches')
    expect(state.chapters.branches).toEqual({ status: 'active', step: 'branches/open-branches' })
    // The chapter that was running keeps its progress untouched, but is paused
    // (dismissed) so only one chapter ever holds status 'active'.
    expect(state.chapters['live-loop']?.status).toBe('dismissed')
    expect(state.chapters['live-loop']?.step).toBe('live-loop/watch-scan')
    expect(scenarioScanArtifact(state)).toEqual({
      scanConfigId: 'sc-1',
      scanJobId: 'job-1',
      startedAt: 1000,
    })
  })

  it('startChapter resumes a dismissed chapter where it left off', () => {
    const state = scenarioReducer(watching(), { type: 'dismissChapter', chapter: 'live-loop' })
    expect(state.activeChapter).toBeNull()

    const resumed = scenarioReducer(state, { type: 'startChapter', chapter: 'live-loop' })
    expect(resumed.chapters['live-loop']?.step).toBe('live-loop/watch-scan')
    expect(resumed.chapters['live-loop']?.status).toBe('active')
  })

  it('startChapter on a completed chapter starts it over', () => {
    let state = scenarioReducer(initialScenarioState(), {
      type: 'startChapter',
      chapter: 'explore',
    })
    for (const step of CHAPTER_STEP_IDS.explore) {
      state = scenarioReducer(state, { type: 'stepCompleted', step })
    }
    expect(state.chapters.explore?.status).toBe('completed')

    const again = scenarioReducer(state, { type: 'startChapter', chapter: 'explore' })
    expect(again.chapters.explore).toEqual({ status: 'active', step: 'explore/visit-coverage' })
  })

  it('restartChapter always starts from the first step', () => {
    const restarted = scenarioReducer(watching(), {
      type: 'restartChapter',
      chapter: 'live-loop',
    })
    expect(restarted.chapters['live-loop']).toEqual({
      status: 'active',
      step: 'live-loop/run-scan',
    })
  })

  it('marks the scenario engaged once the user starts or restarts a chapter', () => {
    expect(initialScenarioState().engaged).toBeUndefined()
    const started = scenarioReducer(initialScenarioState(), {
      type: 'startChapter',
      chapter: 'live-loop',
    })
    expect(started.engaged).toBe(true)
    // Still on the first step — the step alone cannot tell the two apart.
    expect(started.chapters['live-loop']?.step).toBe('live-loop/run-scan')
    expect(
      scenarioReducer(initialScenarioState(), { type: 'restartChapter', chapter: 'live-loop' }).engaged,
    ).toBe(true)
    // Kept through later events, and through a reload.
    const dismissed = scenarioReducer(started, { type: 'dismissChapter', chapter: 'live-loop' })
    expect(dismissed.engaged).toBe(true)
    writeScenarioState(SLUG, dismissed)
    expect(readScenarioState(SLUG).engaged).toBe(true)
  })

  it('dismissChapter on a completed chapter clears the pointer but keeps it completed', () => {
    let state = scenarioReducer(initialScenarioState(), {
      type: 'startChapter',
      chapter: 'explore',
    })
    for (const step of CHAPTER_STEP_IDS.explore) {
      state = scenarioReducer(state, { type: 'stepCompleted', step })
    }
    expect(state.chapters.explore?.status).toBe('completed')

    const dismissed = scenarioReducer(state, { type: 'dismissChapter', chapter: 'explore' })
    expect(dismissed.activeChapter).toBeNull()
    // Putting the victory strip away never demotes a finished chapter — the
    // picker keeps showing it Completed and nextChapterId keeps skipping it.
    expect(dismissed.chapters.explore?.status).toBe('completed')
  })

  it('dismissChapter is per-chapter and clears the active pointer only for itself', () => {
    let state = scenarioReducer(initialScenarioState(), {
      type: 'startChapter',
      chapter: 'variables',
    })
    state = scenarioReducer(state, { type: 'dismissChapter', chapter: 'live-loop' })
    expect(state.activeChapter).toBe('variables')
    expect(state.chapters['live-loop']?.status).toBe('dismissed')

    state = scenarioReducer(state, { type: 'dismissChapter', chapter: 'variables' })
    expect(state.activeChapter).toBeNull()
    expect(isScenarioActive(state)).toBe(false)
  })
})

describe('progress helpers', () => {
  it('reports the step index within the active chapter', () => {
    expect(scenarioStepIndex(initialScenarioState())).toBe(0)
    expect(scenarioStepIndex(watching())).toBe(1)
    expect(scenarioStepIndex(collecting())).toBe(2)
  })

  it('is watching only while a user-started artifact is in flight', () => {
    expect(isScenarioWatching(initialScenarioState())).toBe(false)
    expect(isScenarioWatching(watching())).toBe(true)
    expect(isScenarioWatching(collecting())).toBe(true)
    const landed = scenarioReducer(watching(), { type: 'scanSettled', outcome: 'completed' })
    expect(isScenarioWatching(landed)).toBe(false)
  })

  it('offers the next not-completed chapter in order, and null when all are done', () => {
    let state = initialScenarioState()
    expect(nextChapterId(state)).toBe('edit-event')

    // Complete every chapter; the offer walks the order and then dries up.
    for (const chapter of CHAPTER_IDS) {
      state = scenarioReducer(state, { type: 'startChapter', chapter })
      for (const step of CHAPTER_STEP_IDS[chapter]) {
        state = scenarioReducer(state, { type: 'stepCompleted', step })
      }
      expect(chapterStatus(state, chapter)).toBe('completed')
    }
    expect(nextChapterId(state)).toBeNull()
  })

  it('wraps the next-chapter offer past the end of the order', () => {
    let state = initialScenarioState()
    // Complete everything except edit-event, ending with explore active.
    for (const chapter of CHAPTER_IDS.filter((id) => id !== 'edit-event')) {
      state = scenarioReducer(state, { type: 'startChapter', chapter })
      for (const step of CHAPTER_STEP_IDS[chapter]) {
        state = scenarioReducer(state, { type: 'stepCompleted', step })
      }
    }
    expect(state.activeChapter).toBe('explore')
    expect(nextChapterId(state)).toBe('edit-event')
  })
})

describe('buildChapterSteps and the seeded deep links', () => {
  it('deep-links live-loop steps against the collected artifacts', () => {
    const fresh = buildChapterSteps(SLUG, 'live-loop', initialScenarioState())
    expect(fresh.map((s) => s.id)).toEqual([...CHAPTER_STEP_IDS['live-loop']])
    expect(fresh[0].to).toBe('/p/acme/scans')

    const run = buildChapterSteps(SLUG, 'live-loop', watching())
    expect(at(run, 1).to).toBe('/p/acme/scans/sc-1')

    const ready = scenarioReducer(collecting(), { type: 'collectSettled', outcome: 'success' })
    expect(at(buildChapterSteps(SLUG, 'live-loop', ready), 3).to).toBe('/p/acme/monitoring/metric/m-1')
  })

  it('gives every chapter steps with titles, instructions and a destination', () => {
    for (const chapter of CHAPTER_IDS) {
      const steps = buildChapterSteps(SLUG, chapter, initialScenarioState())
      expect(steps.map((s) => s.id)).toEqual([...CHAPTER_STEP_IDS[chapter]])
      for (const step of steps) {
        expect(step.title).not.toBe('')
        expect(step.instruction).not.toBe('')
        expect(step.to.startsWith(`/p/${SLUG}/`)).toBe(true)
      }
    }
  })

  it('coaches meaning-first search with the curated examples', () => {
    const steps = buildChapterSteps(SLUG, 'explore', initialScenarioState())
    const useSearch = steps.find((step) => step.id === 'explore/use-search')
    expect(useSearch?.instruction).toContain('Ctrl K')
    expect(useSearch?.instruction).toContain('⌘K')
    expect(useSearch?.instruction).toContain('purchase funnel')
    expect(useSearch?.instruction).toContain('money back')
  })

  it('activeScenarioStep returns the active chapter’s current step', () => {
    expect(activeScenarioStep(SLUG, watching()).id).toBe('live-loop/watch-scan')
    const branches = scenarioReducer(initialScenarioState(), {
      type: 'startChapter',
      chapter: 'branches',
    })
    expect(activeScenarioStep(SLUG, branches).id).toBe('branches/open-branches')
  })

  it('buildChapterList carries status and the first-step link for the picker', () => {
    const list = buildChapterList(SLUG, watching())
    expect(list.map((entry) => entry.id)).toEqual([...CHAPTER_IDS])
    expect(at(list, 0).status).toBe('active')
    expect(at(list, 1).status).toBe('not_started')
    expect(at(list, 0).to).toBe('/p/acme/scans')
  })
})

describe('stepCompletedByPath — consecutive steps never share an arrival path', () => {
  // The provider advances on arrival during render, one step per render. Two
  // consecutive steps completed by the same path would skip the second one the
  // moment the first landed, before the user ever saw it.
  const samplePaths = [
    `/p/${SLUG}/events/all/ev-1/edit`,
    `/p/${SLUG}/events/all/ev-1`,
    `/p/${SLUG}/variables`,
    `/p/${SLUG}/branches`,
    `/p/${SLUG}/branches/br-1`,
    `/p/${SLUG}/reconciliation`,
    `/p/${SLUG}/alerting`,
    `/p/${SLUG}/coverage`,
    `/p/${SLUG}/monitoring/event/ev-1`,
    `/p/${SLUG}/monitoring/metric/m-1`,
    `/p/${SLUG}/anomalies`,
    `/p/${SLUG}/overview`,
    `/p/${SLUG}/scans`,
    // Every link the chapters themselves hand out.
    ...CHAPTER_IDS.flatMap((chapterId) =>
      buildChapterSteps(SLUG, chapterId, initialScenarioState()).map((step) => at(step.to.split('?'), 0)),
    ),
  ]

  const pairs = CHAPTER_IDS.flatMap((chapterId) =>
    CHAPTER_STEP_IDS[chapterId].slice(1).map((next, index) => {
      const current = at(CHAPTER_STEP_IDS[chapterId], index)
      return [current, next] as const
    }),
  )

  it.each(pairs)('%s and %s', (current, next) => {
    const shared = samplePaths.filter(
      (pathname) =>
        stepCompletedByPath(SLUG, current, pathname) && stepCompletedByPath(SLUG, next, pathname),
    )
    expect(shared).toEqual([])
  })
})

describe('stepCompletedByPath — arrival completes deep-link steps', () => {
  it.each([
    ['edit-event/open-editor', `/p/${SLUG}/events/all/ev-1/edit`, true],
    ['edit-event/open-editor', `/p/${SLUG}/events`, false],
    ['variables/open-variables', `/p/${SLUG}/variables`, true],
    ['branches/open-branches', `/p/${SLUG}/branches`, true],
    ['reconcile/open-reconciliation', `/p/${SLUG}/reconciliation`, true],
    ['alerting/open-alerting', `/p/${SLUG}/alerting`, true],
    ['explore/visit-coverage', `/p/${SLUG}/coverage`, true],
    ['explore/visit-anomaly', `/p/${SLUG}/monitoring/event/ev-1`, true],
    ['explore/visit-anomaly', `/p/${SLUG}/anomalies`, false],
    // Mutation-notified steps never complete by path.
    ['edit-event/save', `/p/${SLUG}/events/all/ev-1/edit`, false],
    ['explore/use-search', `/p/${SLUG}/overview`, false],
  ] as const)('%s at %s -> %s', (step, pathname, expected) => {
    expect(stepCompletedByPath(SLUG, step, pathname)).toBe(expected)
  })
})

describe('persistence', () => {
  it('round-trips a state carrying both artifacts', () => {
    const ready = scenarioReducer(collecting(), { type: 'collectSettled', outcome: 'success' })
    writeScenarioState(SLUG, ready)
    expect(readScenarioState(SLUG)).toEqual(ready)
  })

  it('round-trips multi-chapter progress', () => {
    let state = scenarioReducer(watching(), { type: 'startChapter', chapter: 'reconcile' })
    state = scenarioReducer(state, { type: 'stepCompleted', step: 'reconcile/open-reconciliation' })
    writeScenarioState(SLUG, state)
    expect(readScenarioState(SLUG)).toEqual(state)
  })

  it('starts fresh when nothing is stored', () => {
    expect(readScenarioState(SLUG)).toEqual(initialScenarioState())
  })

  it('migrates a valid v1 blob onto the live-loop chapter', () => {
    window.localStorage.setItem(
      KEY,
      JSON.stringify({
        v: 1,
        status: 'active',
        step: 'watch-scan',
        scan: { scanConfigId: 'sc-1', scanJobId: 'job-1', startedAt: 1234 },
        hint: 'watch-timeout',
      }),
    )
    const migrated = readScenarioState(SLUG)
    expect(migrated.v).toBe(3)
    expect(migrated.activeChapter).toBe('live-loop')
    expect(migrated.chapters['live-loop']?.step).toBe('live-loop/watch-scan')
    expect(migrated.chapters['live-loop']?.hint).toBe('watch-timeout')
    expect(scenarioScanArtifact(migrated)).toEqual({
      scanConfigId: 'sc-1',
      scanJobId: 'job-1',
      startedAt: 1234,
    })
  })

  it('keeps a dismissed v1 run put away after migration', () => {
    window.localStorage.setItem(
      KEY,
      JSON.stringify({ v: 1, status: 'dismissed', step: 'run-scan' }),
    )
    const migrated = readScenarioState(SLUG)
    expect(migrated.activeChapter).toBeNull()
    expect(migrated.chapters['live-loop']?.status).toBe('dismissed')
  })

  it('restarts legacy v2 edit progress without losing other chapters', () => {
    window.localStorage.setItem(
      KEY,
      JSON.stringify({
        v: 2,
        activeChapter: 'edit-event',
        chapters: {
          'edit-event': {
            status: 'active',
            step: 'edit-event/set-value',
            artifacts: { editorPath: `/p/${SLUG}/events/all/home-screen-view/edit` },
          },
          variables: { status: 'completed', step: 'variables/see-drift' },
        },
      }),
    )

    expect(readScenarioState(SLUG)).toEqual({
      v: 3,
      activeChapter: 'edit-event',
      chapters: {
        'edit-event': { status: 'active', step: 'edit-event/open-editor' },
        variables: { status: 'completed', step: 'variables/see-drift' },
      },
    })
  })

  it('clears completed legacy v2 edit progress that still owns the pointer', () => {
    window.localStorage.setItem(
      KEY,
      JSON.stringify({
        v: 2,
        activeChapter: 'edit-event',
        chapters: {
          'edit-event': {
            status: 'completed',
            step: 'edit-event/save',
            artifacts: { editorPath: `/p/${SLUG}/events/all/home-screen-view/edit` },
          },
          variables: { status: 'completed', step: 'variables/see-drift' },
        },
      }),
    )

    expect(readScenarioState(SLUG)).toEqual({
      v: 3,
      activeChapter: null,
      chapters: {
        variables: { status: 'completed', step: 'variables/see-drift' },
      },
    })
  })

  it.each([
    ['corrupt JSON', 'not json at all'],
    ['a future version', JSON.stringify({ v: 999, activeChapter: null, chapters: {} })],
    ['a v1 blob with an unknown step', JSON.stringify({ v: 1, status: 'active', step: 'teleport' })],
    [
      'a v1 blob with a malformed artifact',
      JSON.stringify({ v: 1, status: 'active', step: 'watch-scan', scan: { scanJobId: 'j' } }),
    ],
    [
      'a v2 blob with an unknown chapter',
      JSON.stringify({
        v: 2,
        activeChapter: 'nope',
        chapters: { nope: { status: 'active', step: 'nope/step' } },
      }),
    ],
    [
      'a v2 blob whose step belongs to another chapter',
      JSON.stringify({
        v: 2,
        activeChapter: 'branches',
        chapters: { branches: { status: 'active', step: 'variables/see-drift' } },
      }),
    ],
    [
      'a v2 blob whose active pointer has no chapter state',
      JSON.stringify({ v: 2, activeChapter: 'branches', chapters: {} }),
    ],
    ['a non-object payload', JSON.stringify('run-scan')],
  ])('reads %s as a fresh scenario', (_label, raw) => {
    window.localStorage.setItem(KEY, raw)
    expect(readScenarioState(SLUG)).toEqual(initialScenarioState())
  })

  it('keeps each project on its own scenario', () => {
    writeScenarioState(SLUG, watching())
    expect(readScenarioState('other')).toEqual(initialScenarioState())
  })
})
