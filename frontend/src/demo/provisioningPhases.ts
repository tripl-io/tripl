/**
 * The phases of a demo-project provision, and how the UI knows which one runs.
 *
 * A create answers 202 with a `seeding` shell and the page re-reads it every
 * second (`projectsApi.createDemo`). A demo is built the way a real project
 * starts: its tracking plan is written, the demo warehouse is scanned, the
 * history is collected from it, and the detectors run over that history. The
 * server names the step it is on in the shell's `generation_stage` — and how
 * far through the history it is in `generation_progress` — so the list shows
 * where the server really is.
 *
 * A server that names no stage (an older one, or a stage this build does not
 * know) gets the old treatment: the UI *estimates* its way through the phases
 * on a timer, labels the list an estimate, and only marks work done once the
 * request itself resolves.
 */

export interface ProvisioningPhase {
  id: string
  label: string
}

export const PROVISIONING_PHASES: readonly ProvisioningPhase[] = [
  { id: 'plan', label: 'Writing the tracking plan' },
  { id: 'scan', label: 'Scanning the demo warehouse' },
  { id: 'history', label: 'Collecting history' },
  { id: 'signals', label: 'Detecting signals' },
] as const

/**
 * The phase a server-reported `generation_stage` names. `init` is the shell
 * waiting for a worker: nothing is written yet, so it is the first phase.
 * Null for no stage, for `cancel_requested`, and for any stage this build does
 * not know — the caller falls back to the estimate then.
 */
export function phaseIndexForStage(stage: string | null | undefined): number | null {
  if (!stage) return null
  if (stage === 'init') return 0
  const index = PROVISIONING_PHASES.findIndex((phase) => phase.id === stage)
  return index === -1 ? null : index
}

/**
 * What a polled demo shell says about its own progress. `percent` is the share
 * of the history collected, 0..100, and is meaningful only while `stage` is
 * `history`.
 */
export interface ProvisioningProgress {
  stage: string | null
  percent: number | null
}

/**
 * The progress a polled shell reports. An older server omits
 * `generation_progress`, and only the history stage carries one.
 */
export function provisioningProgressOf(project: {
  generation_stage?: string | null
  generation_progress?: number | null
}): ProvisioningProgress {
  const raw = project.generation_progress
  const percent =
    typeof raw === 'number' && Number.isFinite(raw) ? Math.min(100, Math.max(0, Math.round(raw))) : null
  return { stage: project.generation_stage ?? null, percent }
}

/** A phase's label, with the server's percentage where it gave one. */
export function phaseLabel(index: number, percent: number | null = null): string {
  const label = PROVISIONING_PHASES[index]?.label ?? 'Working'
  return percent === null ? label : `${label} · ${percent}%`
}

/** Advance the animated phase pointer, clamped to the last (never past it). */
export function nextPhaseIndex(current: number): number {
  return Math.min(current + 1, PROVISIONING_PHASES.length - 1)
}

/**
 * The ONE expected duration of a demo create or reset. A demo is no longer
 * seeded straight into tables: its warehouse is scanned and its history
 * collected through the real pipeline, about a minute end to end. Every other
 * number the UI states or times against derives from this one, so the copy, the
 * phase tick and the API comment cannot disagree with each other.
 */
export const DEMO_PROVISION_EXPECTED_MS = 60_000

/**
 * How often the estimated pointer advances when the server names no stage, in
 * ms: the phases spread across the expected duration, so the pointer reaches
 * the last one about when the server does.
 */
export const PHASE_TICK_MS = DEMO_PROVISION_EXPECTED_MS / PROVISIONING_PHASES.length

/**
 * Past this the wait is no longer normal, and the phase list says so instead
 * of leaving the pointer parked on the last phase with no signal.
 */
export const DEMO_PROVISION_SLOW_MS = DEMO_PROVISION_EXPECTED_MS * 2

/**
 * Building a demo is heavy but bounded — a fixed recipe over a warehouse of a
 * fixed size, not user-sized data — so a create or reset still running after
 * this long is a stall, not slow progress. Without a bound, a dead connection
 * leaves the dialog spinning forever and a page reload is the only way out.
 */
export const DEMO_PROVISION_TIMEOUT_MS = 5 * 60_000

/** "about a minute", "about 2 minutes", "about 10 seconds". */
function aboutDuration(ms: number): string {
  const seconds = Math.round(ms / 1000)
  if (seconds < 60) return `about ${seconds} seconds`
  const minutes = Math.round(seconds / 60)
  return minutes === 1 ? 'about a minute' : `about ${minutes} minutes`
}

/**
 * One wait-time claim, shared by every surface that offers demo generation, so
 * the empty-workspace hero and the progress dialog cannot disagree with each
 * other or with measurement.
 */
export const DEMO_PROVISION_ESTIMATE = aboutDuration(DEMO_PROVISION_EXPECTED_MS)

/** The page's longest wait, in the same words ("5 minutes"). */
export const DEMO_PROVISION_TIMEOUT_LABEL = aboutDuration(DEMO_PROVISION_TIMEOUT_MS).replace(/^about /, '')
