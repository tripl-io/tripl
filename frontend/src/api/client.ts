import { currentOrgSlug } from '@/lib/activeOrg'
import { uid } from '@/lib/uid'

const BASE = '/api/v1'
const BACKEND_UNAVAILABLE_MESSAGE = 'Backend is unavailable. Check that the API server is running and try again.'
export const AUTH_UNAUTHORIZED_EVENT = 'tripl:unauthorized'
/** Fired when the user signs out on purpose. Module state that outlives the
 *  shell (collect watches) listens for it, so the auth provider does not have
 *  to import — and put on the first load — every module that keeps some. */
export const AUTH_SIGNED_OUT_EVENT = 'tripl:signed-out'

/** One entry of a FastAPI 422 validation error `detail` array. */
export interface ApiFieldError {
  loc: (string | number)[]
  msg: string
  type: string
}

export class ApiError extends Error {
  status: number
  /** Structured field errors from a FastAPI 422 response, when present. */
  fields?: ApiFieldError[]
  /** Backend request id (`X-Request-ID` / 500 body) for support references. */
  requestId?: string
  /** Raw structured `detail` body (e.g. merge-gate 409 payloads), when not a string. */
  detail?: unknown
  /**
   * Where to begin single sign-on, when an organization that requires it
   * refused this session (`sso_start` beside the 403's `detail`, F20).
   */
  ssoStart?: string

  constructor(message: string, status: number, requestId?: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.requestId = requestId
  }
}

function emitUnauthorized(path: string, status: number) {
  // 401 means re-authenticate; 403 is an authorization failure on a valid
  // session and must NOT trigger the re-auth flow.
  if (status !== 401 || path.startsWith('/auth') || typeof window === 'undefined') {
    return
  }

  window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT))
}

/** Build a readable message from a FastAPI 422 `detail` array. */
function formatValidationDetail(detail: ApiFieldError[]): string {
  return detail
    .map((item) => {
      // Drop the leading "body"/"query" segment for a tidier path.
      const path = item.loc.filter((seg) => seg !== 'body' && seg !== 'query').join('.')
      return path ? `${path}: ${item.msg}` : item.msg
    })
    .join('; ')
}

function isFieldErrorArray(detail: unknown): detail is ApiFieldError[] {
  return (
    Array.isArray(detail) &&
    detail.every(
      (item) =>
        item != null &&
        typeof item === 'object' &&
        'msg' in item &&
        'loc' in item &&
        Array.isArray((item as { loc: unknown }).loc),
    )
  )
}

/**
 * The first path segments the server serves inside an organization (F20 PR7).
 * Mirrors `ORG_REWRITE_PREFIXES` in backend/src/tripl/middleware/org_context.py
 * minus `settings`, whose per-organization routes come later: a request to
 * `/projects/web/events` goes out as `/orgs/{org}/projects/web/events`.
 * `/orgs/*`, `/auth/*`, `/settings/*` and everything else pass through.
 */
export const ORG_SCOPED_PREFIXES: readonly string[] = [
  'projects',
  'data-sources',
  'users',
  'audit',
  'activity',
  'me',
]

/**
 * `path` addressed inside organization `org`, when its first segment is one
 * {@link ORG_SCOPED_PREFIXES} names. With no organization the path is left as
 * it is and the server acts in the default one, as before organizations.
 */
export function orgScopedPath(path: string, org: string | null = currentOrgSlug()): string {
  if (!org) return path
  const segment = /^\/([^/?#]+)/.exec(path)?.[1]
  if (!segment || !ORG_SCOPED_PREFIXES.includes(segment)) return path
  return `/orgs/${encodeURIComponent(org)}${path}`
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // Only the URL is rewritten: the 401 check below keys off the caller's path
  // (`/auth/…` never raises the re-auth prompt).
  const url = `${BASE}${orgScopedPath(path)}`
  let res: Response
  const headers = new Headers(init?.headers)

  if (init?.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  // Correlate this request with backend logs/traces. The backend echoes the id
  // back on the response and 500 bodies (see RequestIDMiddleware).
  if (!headers.has('X-Request-ID')) {
    headers.set('X-Request-ID', uid())
  }

  try {
    res = await fetch(url, {
      ...init,
      credentials: 'include',
      headers,
    })
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      throw new ApiError(
        'Request to the backend timed out. Try again after the API becomes available.',
        408,
      )
    }
    throw new ApiError(BACKEND_UNAVAILABLE_MESSAGE, 503)
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    // Prefer the backend echo header; fall back to a request_id in a 500 body.
    const requestId =
      res.headers.get('X-Request-ID') ??
      (typeof body.request_id === 'string' ? body.request_id : undefined) ??
      undefined

    if ([502, 503, 504].includes(res.status)) {
      // Use a meaningful body.detail when the gateway/app supplied one.
      const detail = typeof body.detail === 'string' ? body.detail : undefined
      throw new ApiError(detail || BACKEND_UNAVAILABLE_MESSAGE, res.status, requestId)
    }

    emitUnauthorized(path, res.status)

    if (isFieldErrorArray(body.detail)) {
      const error = new ApiError(formatValidationDetail(body.detail), res.status, requestId)
      error.fields = body.detail
      throw error
    }

    const detail = typeof body.detail === 'string' ? body.detail : undefined
    const error = new ApiError(detail || `${res.status} ${res.statusText}`, res.status, requestId)
    if (detail === undefined && body.detail !== undefined) {
      error.detail = body.detail
    }
    if (typeof body.sso_start === 'string') {
      error.ssoStart = body.sso_start
    }
    throw error
  }
  if (res.status === 204) return undefined as T
  return res.json()
}

export const api = {
  // `signal` is TanStack Query's per-query AbortSignal: pass it from a
  // `queryFn: ({ signal }) => …` so a superseded or unmounted query cancels its
  // request instead of queueing behind the browser's per-host connection limit.
  get: <T>(path: string, signal?: AbortSignal) =>
    request<T>(path, signal === undefined ? undefined : { signal }),
  // `signal` lets a caller time out or cancel a long POST (demo provisioning).
  // An aborted fetch surfaces as ApiError(408) — see the AbortError branch in
  // request().
  post: <T>(path: string, data?: unknown, signal?: AbortSignal) =>
    request<T>(path, {
      method: 'POST',
      ...(data === undefined ? {} : { body: JSON.stringify(data) }),
      ...(signal === undefined ? {} : { signal }),
    }),
  put: <T>(path: string, data: unknown) =>
    request<T>(path, { method: 'PUT', body: JSON.stringify(data) }),
  patch: <T>(path: string, data: unknown) =>
    request<T>(path, { method: 'PATCH', body: JSON.stringify(data) }),
  // A body on DELETE is rare, and only for a typed confirmation
  // (`DELETE /orgs/{org}` takes `{ confirm_slug }`).
  del: <T>(path: string, data?: unknown) =>
    request<T>(path, {
      method: 'DELETE',
      ...(data === undefined ? {} : { body: JSON.stringify(data) }),
    }),
}

/** Append `?branch=<id>` (or `&branch=<id>`) to a path. No-op when branchId is null/undefined. */
export function withBranch(path: string, branchId?: string | null): string {
  if (!branchId) return path
  const sep = path.includes('?') ? '&' : '?'
  return `${path}${sep}branch=${encodeURIComponent(branchId)}`
}
