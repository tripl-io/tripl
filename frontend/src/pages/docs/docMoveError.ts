import { ApiError } from '@/api/client'
import { getErrorMessage } from '@/lib/utils'

/** A 409 from move carries the colliding paths; name them. */
export function moveErrorMessage(err: unknown): string {
  if (err instanceof ApiError && err.status === 409 && err.detail && typeof err.detail === 'object') {
    const detail = err.detail as { message?: unknown; collisions?: unknown; paths?: unknown }
    const paths = Array.isArray(detail.collisions) ? detail.collisions : Array.isArray(detail.paths) ? detail.paths : []
    const lead = typeof detail.message === 'string' ? detail.message : 'These paths are already taken'
    if (paths.length > 0) return `${lead}:\n${paths.map(String).join('\n')}`
  }
  return getErrorMessage(err)
}
