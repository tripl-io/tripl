/**
 * Demo-provisioning controller hook (tripl-2su6.9).
 *
 * Owns the single blocking `POST /projects/demo` mutation and the animated
 * phase pointer shown while it runs. Guarantees:
 *  - duplicate-request guard: a second `start()` while a create is in flight is
 *    a no-op (a double-click can't spawn two demos);
 *  - on success: invalidate `['projects']` and route to the new demo's Overview
 *    welcome (NOT Events), unless the caller overrides `onSuccess`;
 *  - on failure (500): expose the error and a `retry()` that runs a FRESH create.
 *
 * Cancelling is a two-part handshake (tripl-jfm3.12). Aborting the fetch only
 * stops the browser reading the response — the server finishes the seed anyway —
 * so `cancel()` also asks the backend to abandon the provision. The server can
 * only do that while the shell is still seeding, so the outcome is reported
 * honestly: either it was stopped, or the demo is going to appear regardless.
 *
 * The mutation deliberately NEVER rejects (tripl-jfm3.13): every outcome comes
 * back as a resolved discriminated union. The app registers a global
 * `MutationCache.onError` that toasts any rejected mutation, which turned a
 * user-initiated cancel into a red "the backend timed out" toast and rendered a
 * genuine 500 twice. Provisioning failures belong in the dialog, once.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ApiError } from '@/api/client'
import { projectsApi, type DemoCancelResult } from '@/api/projects'
import type { Project } from '@/types'
import { DEMO_PROVISION_TIMEOUT_MS } from './provisioningPhases'
import { useEstimatedPhase } from './useEstimatedPhase'
import { projectsKey } from '@/lib/queryKeys'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

export type ProvisioningStatus =
  | 'idle'
  | 'provisioning'
  | 'cancelling'
  | 'cancelled'
  | 'error'
  | 'success'

/** What the server was actually able to do about a cancel request. */
export type CancelOutcome =
  /** The in-flight provision was flagged and deletes itself — nothing is created. */
  | 'stopped'
  /** Too late: the create had finished, so the demo will appear in the list. */
  | 'already-finished'
  /**
   * The server never saw a provision to stop, or the cancel itself could not be
   * delivered: whether a demo exists is unknown, so the user is sent to the list
   * rather than told it "will appear" (DEMO-28).
   */
  | 'unknown'

// Lives with the other provisioning timings; re-exported for existing callers.
export { DEMO_PROVISION_TIMEOUT_MS }

/**
 * Mirrors `demo_service.MAX_DEMOS_PER_CREATOR`. Used only to warn before the
 * request; the backend remains the enforcing side (it answers 409).
 */
export const MAX_DEMOS_PER_CREATOR = 3

/** Every terminal shape a create can reach. Resolved, never thrown. `run`
 *  names the attempt it belongs to, so an attempt the user has since closed
 *  cannot report back into the next one. */
type CreateOutcome =
  | { kind: 'created'; project: Project; run: number }
  | { kind: 'cancelled'; run: number }
  /** Stopped by a cancel this tab did not send (the same user, another tab). */
  | { kind: 'cancelled-elsewhere'; run: number }
  | { kind: 'failed'; error: unknown; run: number }

/**
 * The server's answer to a create that a cancel stopped: a 409 like the demo
 * limit, told apart by its detail (demo_service.create_demo_project). The
 * cancel is per creator, so one sent from another tab stops this tab's create
 * too — and this tab must say "cancelled", not "limit reached".
 */
function isCancelledElsewhere(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409 && /provisioning was cancelled/i.test(error.message)
}

/**
 * Reads the server's cancel answer. `cancelled: false` covers both "had
 * already finished" and "never started" (DEMO-28); the server says which in
 * `state` (`finished` when a demo of this user's became ready moments ago,
 * `none` otherwise), and only `finished` lets the UI promise the demo will
 * appear. An answer without it — an older server — names no outcome it
 * cannot know.
 */
function cancelOutcomeOf(result: DemoCancelResult): CancelOutcome {
  if (result.cancelled) return 'stopped'
  // Read loosely: the generated type gains `state` when api.gen.ts is next
  // regenerated, and an older server omits it.
  const state = (result as DemoCancelResult & { state?: unknown }).state
  return state === 'finished' ? 'already-finished' : 'unknown'
}

export interface DemoProvisioningController {
  status: ProvisioningStatus
  /** Index into PROVISIONING_PHASES of the currently-animating phase. */
  phaseIndex: number
  error: unknown
  project: Project | null
  /** True when the create was aborted by the timeout rather than rejected. */
  timedOut: boolean
  /** Set once a cancel has been answered by the server; null otherwise. */
  cancelOutcome: CancelOutcome | null
  /** Begin a create. No-op while one is already in flight (duplicate guard). */
  start: () => void
  /** Run a fresh create after a failure. */
  retry: () => void
  /** Abandon an in-flight create: abort the request and ask the server to stop. */
  cancel: () => void
  /** Return to idle and clear any error/result (e.g. closing the dialog). */
  reset: () => void
}

export function useDemoProvisioning(options?: {
  onSuccess?: (project: Project) => void
  /** Overridable so tests can exercise the timeout without a fake clock. */
  timeoutMs?: number
}): DemoProvisioningController {
  const timeoutMs = options?.timeoutMs ?? DEMO_PROVISION_TIMEOUT_MS
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [project, setProject] = useState<Project | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [cancelling, setCancelling] = useState(false)
  const [cancelOutcome, setCancelOutcome] = useState<CancelOutcome | null>(null)
  // Synchronous in-flight flag: state (`isPending`) updates a render later, so a
  // second click in the same tick would still see the old value. The ref closes
  // that race so exactly one create is ever dispatched.
  const inFlightRef = useRef(false)
  const abortRef = useRef<AbortController | null>(null)
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Set synchronously by cancel(), before the abort, so the settled request can
  // tell a deliberate abandon from a timeout (both surface as ApiError(408)).
  const cancelRequestedRef = useRef(false)
  const mountedRef = useRef(true)
  // Bumped by start() and reset(). Closing the dialog while a cancel or the
  // aborted create is still settling (DEMO-6) used to let that late answer
  // reopen it — "cancelled", or a 408 read as "Demo generation failed" —
  // after the user had dismissed it.
  const runRef = useRef(0)
  const onSuccess = options?.onSuccess

  const clearTimer = useCallback(() => {
    if (timeoutRef.current !== null) {
      clearTimeout(timeoutRef.current)
      timeoutRef.current = null
    }
  }, [])

  const mutation = useMutation({
    mutationFn: async (): Promise<CreateOutcome> => {
      const run = runRef.current
      const controller = new AbortController()
      abortRef.current = controller
      clearTimer()
      timeoutRef.current = setTimeout(() => controller.abort(), timeoutMs)
      try {
        return { kind: 'created', project: await projectsApi.createDemo(controller.signal), run }
      } catch (caught) {
        if (cancelRequestedRef.current || run !== runRef.current) return { kind: 'cancelled', run }
        if (isCancelledElsewhere(caught)) return { kind: 'cancelled-elsewhere', run }
        return { kind: 'failed', error: caught, run }
      }
    },
    // react-query keeps the observer options current each render, so this reads
    // the latest onSuccess / navigate.
    onSuccess: (outcome) => {
      // Created after the user closed the dialog: the list still has to learn
      // about it, but nothing may navigate or reopen the dialog.
      if (outcome.run !== runRef.current) {
        if (outcome.kind === 'created') void queryClient.invalidateQueries({ queryKey: projectsKey() })
        return
      }
      if (outcome.kind === 'failed') {
        setError(outcome.error)
        // A failure is not proof that nothing was created (DEMO-5): a dropped
        // connection or a timeout can hide a create the server finished, and a
        // 409 says the list this tab holds is out of date. Refresh it.
        void queryClient.invalidateQueries({ queryKey: projectsKey() })
        return
      }
      if (outcome.kind === 'cancelled') return
      if (outcome.kind === 'cancelled-elsewhere') {
        setCancelOutcome('stopped')
        void queryClient.invalidateQueries({ queryKey: projectsKey() })
        return
      }
      setProject(outcome.project)
      void queryClient.invalidateQueries({ queryKey: projectsKey() })
      if (onSuccess) {
        onSuccess(outcome.project)
      } else {
        void navigate(projectPath(currentOrgSlug(), outcome.project.slug, '/overview'))
      }
    },
    onSettled: (outcome) => {
      // A superseded run must not release the guard, timer or abort handle
      // the current one holds.
      if (outcome && outcome.run !== runRef.current) return
      inFlightRef.current = false
      clearTimer()
      abortRef.current = null
    },
  })

  const { isPending, mutate, reset: resetMutation } = mutation

  // Animate through the expected phases while the request is blocking. There is
  // no server-side stage feed, so this is a timed best-effort narration — the
  // dialog labels it as an estimate rather than asserting completed work
  // (tripl-jfm3.16). The same hook drives the reset dialog (DEMO-22), and it
  // starts over at phase 0 every time a create begins.
  const phaseIndex = useEstimatedPhase(isPending)

  // Abandoning the page must not leave a timer alive to fire against a request
  // nobody is watching any more.
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      clearTimer()
      abortRef.current?.abort()
    }
  }, [clearTimer])

  const start = useCallback(() => {
    if (inFlightRef.current) return
    inFlightRef.current = true
    runRef.current += 1
    cancelRequestedRef.current = false
    setCancelling(false)
    setCancelOutcome(null)
    setError(null)
    setProject(null)
    resetMutation()
    mutate()
  }, [mutate, resetMutation])

  const cancel = useCallback(() => {
    if (!inFlightRef.current) return
    const run = runRef.current
    cancelRequestedRef.current = true
    inFlightRef.current = false
    clearTimer()
    abortRef.current?.abort()
    setCancelling(true)
    void projectsApi
      .cancelDemo()
      .then(cancelOutcomeOf)
      // A cancel we could not deliver is not a cancel: never claim the create
      // was stopped — or that it finished — when we do not know either.
      .catch((): CancelOutcome => 'unknown')
      .then((outcome: CancelOutcome) => {
        // Whatever the answer, the list may have changed under it.
        void queryClient.invalidateQueries({ queryKey: projectsKey() })
        if (!mountedRef.current || run !== runRef.current) return
        setCancelling(false)
        setCancelOutcome(outcome)
      })
  }, [clearTimer, queryClient])

  const reset = useCallback(() => {
    runRef.current += 1
    inFlightRef.current = false
    cancelRequestedRef.current = false
    setCancelling(false)
    setCancelOutcome(null)
    setError(null)
    setProject(null)
    clearTimer()
    abortRef.current?.abort()
    resetMutation()
  }, [clearTimer, resetMutation])

  const status: ProvisioningStatus =
    cancelOutcome !== null
      ? 'cancelled'
      : cancelling
        ? 'cancelling'
        : isPending
          ? 'provisioning'
          : error !== null
            ? 'error'
            : project !== null
              ? 'success'
              : 'idle'

  // The client maps an aborted fetch to ApiError(408); a cancel resolves to the
  // cancelled branch instead, so a 408 that reaches here is the timeout firing.
  const timedOut = status === 'error' && error instanceof ApiError && error.status === 408

  return {
    status,
    phaseIndex,
    error,
    project,
    timedOut,
    cancelOutcome,
    start,
    retry: start,
    cancel,
    reset,
  }
}
