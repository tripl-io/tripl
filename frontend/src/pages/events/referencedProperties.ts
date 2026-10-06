import type { EventFieldValue, Variable } from '@/types'
import { resolveTemplateTokens } from './utils'

/** A property an event's field values name through `${…}`, off its list. */
export interface ReferencedProperty {
  variableId: string
  name: string
  /** Every distinct token that named it, as written: `${property.spot_id}`. */
  tokens: string[]
}

/**
 * The properties an event's saved field values reference and its property
 * list (F23) does not carry yet, in order of first appearance.
 *
 * Resolution is resolveTemplateTokens alone — name, source_name, bindings —
 * the same call the form's "Unknown property token" hint makes, so a token is
 * offered here exactly when the form above does not warn about it. A scan's
 * variable_values contexts are deliberately not read: matching on their
 * source_column would offer tokens the form still calls unknown.
 */
export function referencedProperties(
  fieldValues: readonly EventFieldValue[],
  variables: Variable[],
  listed: ReadonlySet<string>,
): ReferencedProperty[] {
  const byId = new Map<string, ReferencedProperty>()
  for (const fieldValue of fieldValues) {
    for (const { token, variable } of resolveTemplateTokens(fieldValue.value, variables)) {
      if (!variable || listed.has(variable.id)) continue
      const known = byId.get(variable.id)
      if (!known) {
        byId.set(variable.id, { variableId: variable.id, name: variable.name, tokens: [token] })
      } else if (!known.tokens.includes(token)) {
        byId.set(variable.id, { ...known, tokens: [...known.tokens, token] })
      }
    }
  }
  return [...byId.values()]
}
