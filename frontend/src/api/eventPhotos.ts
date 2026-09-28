import type { EventPhoto, EventPhotoComment, PhotoLimits } from '../types'
import { uid } from '@/lib/uid'
import { api, ApiError, AUTH_UNAUTHORIZED_EVENT, orgScopedPath } from './client'

const BASE = '/api/v1'

/**
 * The address to load a photo's file from. The server builds `photo.url` as an
 * org-less `/api/v1/projects/{slug}/…/file` (tripl-0chm), which a cookie
 * session resolves in the default organization, so an `<img>` in any other one
 * would load the default organization's same-slug project, or 404. This applies
 * the client's organization rewrite to it; an already org-qualified URL, or one
 * outside the API, is returned as it is.
 */
export function photoFileUrl(url: string): string {
  const apiPrefix = `${BASE}/`
  if (!url.startsWith(apiPrefix)) return url
  return `${BASE}${orgScopedPath(url.slice(BASE.length))}`
}

/**
 * POST one file with upload progress.
 *
 * The one call here that cannot go through `api`: the shared client
 * JSON-encodes every body and `fetch` reports no upload progress, while a photo
 * is a multipart body worth a progress bar. It keeps the client's contract —
 * `X-Request-ID` out, `ApiError` back, the re-auth prompt on a 401 — so a
 * failed upload reads like every other failed request (EVT-28).
 */
function uploadWithProgress<T>(
  path: string,
  body: FormData,
  onProgress?: (fraction: number) => void,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    // The same organization rewrite `api` applies (F20 PR7).
    xhr.open('POST', `${BASE}${orgScopedPath(path)}`)
    xhr.withCredentials = true
    xhr.setRequestHeader('X-Request-ID', uid())
    xhr.upload.onprogress = event => {
      if (event.lengthComputable && event.total > 0) onProgress?.(event.loaded / event.total)
    }
    xhr.onerror = () => {
      reject(new ApiError('Backend is unavailable. Check that the API server is running and try again.', 503))
    }
    xhr.onload = () => {
      let parsed: unknown
      try {
        parsed = xhr.responseText ? JSON.parse(xhr.responseText) : null
      } catch {
        parsed = null
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(1)
        resolve(parsed as T)
        return
      }
      if (xhr.status === 401) window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT))
      const detail =
        parsed && typeof parsed === 'object' && typeof (parsed as { detail?: unknown }).detail === 'string'
          ? (parsed as { detail: string }).detail
          : undefined
      reject(
        new ApiError(
          detail || `${xhr.status} ${xhr.statusText}`,
          xhr.status,
          xhr.getResponseHeader('X-Request-ID') ?? undefined,
        ),
      )
    }
    xhr.send(body)
  })
}

export const eventPhotosApi = {
  /** The instance's upload limit (an owner setting any signed-in user may read). */
  limits: (): Promise<PhotoLimits> => api.get<PhotoLimits>('/settings/photo-limits'),

  list: (slug: string, eventId: string): Promise<EventPhoto[]> =>
    api.get<EventPhoto[]>(`/projects/${slug}/events/${eventId}/photos`),

  upload: (
    slug: string,
    eventId: string,
    file: File,
    onProgress?: (fraction: number) => void,
  ): Promise<EventPhoto> => {
    const form = new FormData()
    form.append('file', file)
    return uploadWithProgress<EventPhoto>(`/projects/${slug}/events/${eventId}/photos`, form, onProgress)
  },

  delete: async (slug: string, eventId: string, photoId: string): Promise<void> => {
    await api.del<void>(`/projects/${slug}/events/${eventId}/photos/${photoId}`)
  },

  reorder: (slug: string, eventId: string, photoIds: string[]): Promise<EventPhoto[]> =>
    api.patch<EventPhoto[]>(`/projects/${slug}/events/${eventId}/photos/reorder`, {
      photo_ids: photoIds,
    }),

  attachFigma: (slug: string, eventId: string, url: string, title = ''): Promise<EventPhoto> =>
    api.post<EventPhoto>(`/projects/${slug}/events/${eventId}/photos/figma`, { url, title }),

  listComments: (slug: string, eventId: string, photoId: string): Promise<EventPhotoComment[]> =>
    api.get<EventPhotoComment[]>(`/projects/${slug}/events/${eventId}/photos/${photoId}/comments`),

  createComment: (
    slug: string,
    eventId: string,
    photoId: string,
    body: string,
    parentId: string | null = null,
  ): Promise<EventPhotoComment> =>
    api.post<EventPhotoComment>(
      `/projects/${slug}/events/${eventId}/photos/${photoId}/comments`,
      { body, parent_id: parentId },
    ),

  deleteComment: async (
    slug: string,
    eventId: string,
    photoId: string,
    commentId: string,
  ): Promise<void> => {
    await api.del<void>(
      `/projects/${slug}/events/${eventId}/photos/${photoId}/comments/${commentId}`,
    )
  },
}
