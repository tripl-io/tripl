/**
 * Expected phases of a synchronous demo-project provision.
 *
 * Provisioning is a single blocking `POST /projects/demo` with NO mid-request
 * stage polling — the response is terminal (ready or 500). The progress UI
 * therefore *estimates* its way through these expected phases on a timer so the
 * wait reads as staged work rather than an indefinite spinner. The client cannot
 * know which phase the server is really in, so the UI presents the list as an
 * estimate and only marks work done once the request itself resolves.
 * The real outcome comes from the request resolving, not from
 * any phase reaching the end.
 */

export interface ProvisioningPhase {
  id: string
  label: string
}

export const PROVISIONING_PHASES: readonly ProvisioningPhase[] = [
  { id: 'workspace', label: 'Creating workspace' },
  { id: 'events', label: 'Seeding events' },
  { id: 'metrics', label: 'Collecting metrics' },
  { id: 'monitors', label: 'Configuring monitors' },
  { id: 'finalizing', label: 'Finalizing' },
] as const

/** Advance the animated phase pointer, clamped to the last (never past it). */
export function nextPhaseIndex(current: number): number {
  return Math.min(current + 1, PROVISIONING_PHASES.length - 1)
}

/**
 * The ONE measured duration of a demo create or reset: end to end it
 * is ~9-11 s locally, so 10 s. Every other number the UI states or times
 * against derives from this one, so the copy, the phase tick and the API
 * comment can no longer disagree with each other (they said 10-15, 9-11 and
 * 5-8 s).
 */
export const DEMO_PROVISION_EXPECTED_MS = 10_000

/**
 * How often the progress UI advances to the next expected phase, in ms: the
 * five phases spread across the expected duration, so the pointer reaches
 * "Finalizing" about when the server does.
 */
export const PHASE_TICK_MS = DEMO_PROVISION_EXPECTED_MS / PROVISIONING_PHASES.length

/**
 * Past this the wait is no longer normal, and the phase list says so instead
 * of leaving the pointer parked on "Finalizing" with no signal.
 */
export const DEMO_PROVISION_SLOW_MS = DEMO_PROVISION_EXPECTED_MS * 2

/**
 * Seeding is heavy but bounded — it is a fixed recipe, not user-sized data — so
 * a create or reset still running after this long is a stall, not slow
 * progress. Without a bound, a dead connection leaves the dialog spinning
 * forever and a page reload is the only way out.
 */
export const DEMO_PROVISION_TIMEOUT_MS = 90_000

/**
 * One wait-time claim, shared by every surface that offers demo generation, so
 * the empty-workspace hero and the progress dialog cannot disagree with each
 * other or with measurement.
 */
export const DEMO_PROVISION_ESTIMATE = `about ${Math.round(DEMO_PROVISION_EXPECTED_MS / 1000)} seconds`
