/**
 * Test-only constructors for v3 scenario states. Page tests seed
 * localStorage with these instead of hand-writing the nested chapter record —
 * one place to change if the persisted shape moves again.
 */

import type {
  ChapterId,
  ChapterState,
  ScenarioHint,
  ScenarioMetricArtifact,
  ScenarioScanArtifact,
  ScenarioState,
  ScenarioStepId,
} from './scenarioModel'

interface LiveLoopOptions {
  scan?: ScenarioScanArtifact
  metric?: ScenarioMetricArtifact
  hint?: ScenarioHint
  status?: ChapterState['status']
}

/** A v3 state with only the live-loop chapter, on `step`, active by default. */
export function liveLoopState(
  step: ScenarioStepId,
  { scan, metric, hint, status = 'active' }: LiveLoopOptions = {},
): ScenarioState {
  const artifacts: Record<string, string> = {}
  if (scan) {
    artifacts.scanConfigId = scan.scanConfigId
    artifacts.scanJobId = scan.scanJobId
    artifacts.scanStartedAt = String(scan.startedAt)
  }
  if (metric) {
    artifacts.metricId = metric.metricId
    artifacts.metricStartedAt = String(metric.startedAt)
  }
  return {
    v: 3,
    activeChapter: status === 'dismissed' ? null : 'live-loop',
    chapters: {
      'live-loop': {
        status,
        step,
        ...(Object.keys(artifacts).length > 0 ? { artifacts } : {}),
        ...(hint ? { hint } : {}),
      },
    },
  }
}

/** A v3 state with one arbitrary chapter active on `step`. */
export function chapterState(
  chapter: ChapterId,
  step: ScenarioStepId,
  status: ChapterState['status'] = 'active',
): ScenarioState {
  return {
    v: 3,
    activeChapter: status === 'dismissed' ? null : chapter,
    chapters: { [chapter]: { status, step } },
  }
}
