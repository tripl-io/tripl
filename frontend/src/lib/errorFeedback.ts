import type { Mutation, Query } from '@tanstack/react-query'
import { toast } from 'sonner'

import { ApiError } from '@/api/client'
import { getErrorMessage } from '@/lib/utils'

/**
 * The one opt-out from the global error toast.
 *
 * `silent: true` says "this query or mutation shows its own error in context",
 * so the backstop stays quiet instead of saying the same thing a second time
 * (a failed save showing both an inline ErrorState and a toast), and polls that
 * promise to be silent stay silent.
 */
export interface ErrorFeedbackMeta extends Record<string, unknown> {
  silent?: boolean
}

declare module '@tanstack/react-query' {
  interface Register {
    queryMeta: ErrorFeedbackMeta
    mutationMeta: ErrorFeedbackMeta
  }
}

/** `meta` for a query or mutation whose component renders its own error. */
export const SILENT_ERROR_META: ErrorFeedbackMeta = { silent: true }

/**
 * A stable toast id per distinct failure, so sonner replaces a repeat instead
 * of stacking it.
 *
 * Keyed on status and message and NOT on the request id: every failed poll of
 * the same outage carries a fresh request id, and keying on it is exactly the
 * "one new toast per query per interval" this exists to stop.
 */
export function errorToastId(error: unknown): string {
  const status = error instanceof ApiError ? error.status : 'client'
  return `error:${status}:${getErrorMessage(error)}`
}

/**
 * The error toast every backstop raises: a 401 stays quiet, a request id is
 * quoted for support, and a repeat replaces the toast instead of stacking.
 *
 * Exported for a `SILENT_ERROR_META` write that has no inline place to report
 * but wants its message reworded (alerting strips Pydantic's "Value error, "
 * prefix): `formatMessage` rewrites the text and nothing else, so the
 * toast keeps the backstop's behaviour. The dedupe id stays keyed on the raw
 * error, so the same failure still replaces its own toast.
 */
export function surfaceError(
  error: unknown,
  formatMessage: (message: string) => string = message => message,
): void {
  // A 401 triggers the dedicated re-auth flow (see AUTH_UNAUTHORIZED_EVENT); a
  // toast there would be noise on top of the redirect.
  if (error instanceof ApiError && error.status === 401) return
  const reference =
    error instanceof ApiError && error.requestId ? `\nReference: ${error.requestId}` : ''
  toast.error(`${formatMessage(getErrorMessage(error))}${reference}`, {
    id: errorToastId(error),
  })
}

/**
 * QueryCache backstop. Skips silent queries and background refetches: a query
 * that already has data is still showing it, so a failed refresh of it (a poll
 * during an outage, say) is not news worth a toast every interval.
 */
export function surfaceQueryError(error: unknown, query: Query<unknown, unknown, unknown>) {
  if (query.meta?.silent) return
  if (query.state.data !== undefined) return
  surfaceError(error)
}

/** MutationCache backstop. Skips mutations that render their own error. */
export function surfaceMutationError(
  error: unknown,
  _variables: unknown,
  _onMutateResult: unknown,
  mutation: Mutation<unknown, unknown, unknown>,
) {
  if (mutation.meta?.silent) return
  surfaceError(error)
}
