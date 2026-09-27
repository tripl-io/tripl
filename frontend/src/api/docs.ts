import { uid } from '@/lib/uid'
import { api, ApiError, AUTH_UNAUTHORIZED_EVENT } from './client'
import type {
  DocBacklinksResponse,
  DocBundle,
  DocFileResponse,
  DocFolderDeleteResponse,
  DocImportMode,
  DocImportRequest,
  DocImportResult,
  DocLinkKind,
  DocLinkResolution,
  DocMoveRequest,
  DocMoveResponse,
  DocRevisionDetail,
  DocRevisionListResponse,
  DocScope,
  DocSearchResponse,
  DocTreeResponse,
  DocWriteRequest,
  DocWriteResponse,
} from '@/types/docs'

const BASE = '/api/v1'

function docsPath(slug: string, suffix = ''): string {
  return `/projects/${encodeURIComponent(slug)}/docs${suffix}`
}

function query(params: Record<string, string | number | boolean | null | undefined>): string {
  const sp = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined) continue
    sp.set(key, String(value))
  }
  const text = sp.toString()
  return text ? `?${text}` : ''
}

/**
 * The two calls that cannot go through `api`: a zip download (a Blob, not
 * JSON) and a multipart zip upload (the shared client JSON-encodes bodies).
 * They keep the client's contract — `X-Request-ID` out, `ApiError` back with a
 * structured `detail`, the re-auth prompt on a 401.
 */
async function rawRequest(path: string, init: RequestInit): Promise<Response> {
  const headers = new Headers(init.headers)
  headers.set('X-Request-ID', uid())
  let res: Response
  try {
    res = await fetch(`${BASE}${path}`, { ...init, headers, credentials: 'include' })
  } catch {
    throw new ApiError('Backend is unavailable. Check that the API server is running and try again.', 503)
  }
  if (res.ok) return res
  if (res.status === 401 && typeof window !== 'undefined') {
    window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT))
  }
  const body: unknown = await res.json().catch(() => ({}))
  const detail = body && typeof body === 'object' ? (body as { detail?: unknown }).detail : undefined
  const error = new ApiError(
    typeof detail === 'string' ? detail : `${res.status} ${res.statusText}`,
    res.status,
    res.headers.get('X-Request-ID') ?? undefined,
  )
  if (typeof detail !== 'string' && detail !== undefined) error.detail = detail
  throw error
}

/** Serialise `ref=kind:target` pairs for the live-preview link resolver. */
export function linkRefQuery(refs: readonly string[]): string {
  const sp = new URLSearchParams()
  for (const ref of refs) sp.append('ref', ref)
  return sp.toString()
}

export interface DocImportParams {
  scope: DocScope
  mode: DocImportMode
  dryRun: boolean
}

/** Docs catalog (F22). Reads are open to members; writes need an editor. */
export const docsApi = {
  tree: (slug: string, signal?: AbortSignal) =>
    api.get<DocTreeResponse>(docsPath(slug), signal),

  read: (slug: string, scope: DocScope, path: string, signal?: AbortSignal) =>
    api.get<DocFileResponse>(docsPath(slug, `/file${query({ scope, path })}`), signal),

  write: (slug: string, scope: DocScope, path: string, body: DocWriteRequest) =>
    api.put<DocWriteResponse>(docsPath(slug, `/file${query({ scope, path })}`), body),

  remove: (slug: string, scope: DocScope, path: string) =>
    api.del<void>(docsPath(slug, `/file${query({ scope, path })}`)),

  removeFolder: (slug: string, scope: DocScope, prefix: string) =>
    api.del<DocFolderDeleteResponse>(docsPath(slug, `/folder${query({ scope, path: prefix })}`)),

  move: (slug: string, body: DocMoveRequest) =>
    api.post<DocMoveResponse>(docsPath(slug, '/move'), body),

  revisions: (slug: string, scope: DocScope, path: string, signal?: AbortSignal) =>
    api.get<DocRevisionListResponse>(docsPath(slug, `/revisions${query({ scope, path })}`), signal),

  revision: (slug: string, revisionId: string, signal?: AbortSignal) =>
    api.get<DocRevisionDetail>(docsPath(slug, `/revisions/${encodeURIComponent(revisionId)}`), signal),

  restore: (slug: string, revisionId: string, message = '') =>
    api.post<DocWriteResponse>(
      docsPath(slug, `/revisions/${encodeURIComponent(revisionId)}/restore`),
      { message },
    ),

  search: (
    slug: string,
    params: { q: string; scope?: DocScope; limit?: number },
    signal?: AbortSignal,
  ) => api.get<DocSearchResponse>(docsPath(slug, `/search${query(params)}`), signal),

  backlinks: (
    slug: string,
    params: { kind: DocLinkKind; name: string; qualifier?: string | null },
    signal?: AbortSignal,
  ) => api.get<DocBacklinksResponse>(docsPath(slug, `/backlinks${query(params)}`), signal),

  /** Resolve `kind:target` refs (kind: event | event-type | field). ≤200. */
  links: (slug: string, refs: readonly string[], signal?: AbortSignal) =>
    refs.length === 0
      ? Promise.resolve<DocLinkResolution[]>([])
      : api.get<DocLinkResolution[]>(docsPath(slug, `/links?${linkRefQuery(refs)}`), signal),

  exportJson: (slug: string, scope: DocScope) =>
    api.get<DocBundle>(docsPath(slug, `/export${query({ scope, format: 'json' })}`)),

  exportZip: async (slug: string, scope: DocScope): Promise<{ blob: Blob; filename: string }> => {
    const res = await rawRequest(docsPath(slug, `/export${query({ scope, format: 'zip' })}`), {
      method: 'GET',
    })
    const disposition = res.headers.get('Content-Disposition') ?? ''
    const match = /filename="?([^";]+)"?/.exec(disposition)
    return { blob: await res.blob(), filename: match?.[1] ?? `${slug}-docs.zip` }
  },

  importJson: (slug: string, params: DocImportParams, body: DocImportRequest) =>
    api.post<DocImportResult>(
      docsPath(
        slug,
        `/import${query({ scope: params.scope, mode: params.mode, dry_run: params.dryRun })}`,
      ),
      body,
    ),

  importZip: async (
    slug: string,
    params: DocImportParams & { keepRoot: boolean },
    file: Blob,
  ): Promise<DocImportResult> => {
    const form = new FormData()
    form.append('file', file)
    const res = await rawRequest(
      docsPath(
        slug,
        `/import/zip${query({
          scope: params.scope,
          mode: params.mode,
          dry_run: params.dryRun,
          keep_root: params.keepRoot,
        })}`,
      ),
      { method: 'POST', body: form },
    )
    return (await res.json()) as DocImportResult
  },
}
