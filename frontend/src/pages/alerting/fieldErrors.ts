import { ApiError } from '@/api/client'
import { stripValueErrorPrefix } from '@/lib/alertStatus'
import { getErrorMessage } from '@/lib/utils'
import { fieldErrorId } from '@/lib/fieldErrors'

/**
 * A server rejection, split into what belongs beside an input and what does not.
 *
 * The alerting dialogs used to print `getErrorMessage(error)` under the form:
 * the flattened `loc: msg` string, with Pydantic's "Value error, " prefix and a
 * snake_case path ("chat_id: Value error, Telegram chat_id is required"), and
 * the offending input never highlighted although `ApiError.fields` carried
 * exactly which one it was.
 */
export interface SplitFieldErrors<K extends string> {
  /** One message per input the form knows, prefix stripped. */
  fields: Partial<Record<K, string>>
  /** Everything with no input to sit beside, or null when there is nothing. */
  message: string | null
}

/**
 * Split an error from a create/update request.
 *
 * `known` are the payload keys the form renders an input for; `labels` names
 * any other key the API may point at, so a leftover reads "Name: …" rather than
 * "name: Value error, …". An error that is not a FastAPI 422 becomes the
 * message alone, prefix stripped.
 */
export function splitApiFieldErrors<K extends string>(
  error: unknown,
  known: readonly K[],
  labels: Readonly<Record<string, string>> = {},
): SplitFieldErrors<K> {
  if (!(error instanceof ApiError) || !error.fields?.length) {
    return {
      fields: {},
      message: error ? stripValueErrorPrefix(getErrorMessage(error)) : null,
    }
  }
  const knownKeys = new Set<string>(known)
  const fields: Partial<Record<K, string>> = {}
  const leftovers: string[] = []
  for (const item of error.fields) {
    const path = item.loc.filter(segment => segment !== 'body' && segment !== 'query')
    const head = path[0]
    const msg = stripValueErrorPrefix(item.msg)
    if (typeof head === 'string' && knownKeys.has(head)) {
      const key = head as K
      // The first message for an input wins; a second one for the same input
      // is appended, so nothing the server said is dropped.
      fields[key] = fields[key] ? `${fields[key]} ${msg}` : msg
      continue
    }
    if (head === undefined) {
      // A model-level validator (`loc: ['body']`) names no field: the sentence
      // is the whole message.
      leftovers.push(msg)
      continue
    }
    const label = typeof head === 'string' ? labels[head] ?? head.replace(/_/g, ' ') : String(head)
    leftovers.push(`${label}: ${msg}`)
  }
  return { fields, message: leftovers.length > 0 ? leftovers.join(' ') : null }
}

/** The id of the message element {@link FieldError} renders for an input. */
export { fieldErrorId }

/** `aria-invalid` + `aria-describedby` for an input, when it has an error. */
export function fieldErrorProps(inputId: string, message: string | null | undefined) {
  return message
    ? { 'aria-invalid': true as const, 'aria-describedby': fieldErrorId(inputId) }
    : {}
}
