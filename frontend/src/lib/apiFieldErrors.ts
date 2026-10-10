import { ApiError } from '@/api/client'
import { getErrorMessage } from '@/lib/utils'

/**
 * A server rejection, split into what belongs beside an input and what does not.
 *
 * `getErrorMessage(error)` alone is the 422 flattened into one line, with a
 * snake_case path in front of each sentence ("new_password: Password must…")
 * and the offending input never marked, although `ApiError.fields` says
 * exactly which one it was. The API client has already dropped Pydantic's
 * "Value error, " prefix from every message.
 */
export interface SplitFieldErrors<K extends string> {
  /** One message per input the form knows. */
  fields: Partial<Record<K, string>>
  /** Everything with no input to sit beside, or null when there is nothing. */
  message: string | null
}

/**
 * Split an error from a create/update request.
 *
 * `known` are the payload keys the form renders an input for; `labels` names
 * any other key the API may point at, so a leftover reads "Name: …" rather than
 * "name: …". An error that is not a FastAPI 422 becomes the message alone.
 */
export function splitApiFieldErrors<K extends string>(
  error: unknown,
  known: readonly K[],
  labels: Readonly<Record<string, string>> = {},
): SplitFieldErrors<K> {
  if (!(error instanceof ApiError) || !error.fields?.length) {
    return { fields: {}, message: error ? getErrorMessage(error) : null }
  }
  const knownKeys = new Set<string>(known)
  const fields: Partial<Record<K, string>> = {}
  const leftovers: string[] = []
  for (const item of error.fields) {
    const path = item.loc.filter(segment => segment !== 'body' && segment !== 'query')
    const head = path[0]
    if (typeof head === 'string' && knownKeys.has(head)) {
      const key = head as K
      // The first message for an input wins; a second one for the same input
      // is appended, so nothing the server said is dropped.
      fields[key] = fields[key] ? `${fields[key]} ${item.msg}` : item.msg
      continue
    }
    if (head === undefined) {
      // A model-level validator (`loc: ['body']`) names no field: the sentence
      // is the whole message.
      leftovers.push(item.msg)
      continue
    }
    const label = typeof head === 'string' ? labels[head] ?? head.replace(/_/g, ' ') : String(head)
    leftovers.push(`${label}: ${item.msg}`)
  }
  return { fields, message: leftovers.length > 0 ? leftovers.join(' ') : null }
}
