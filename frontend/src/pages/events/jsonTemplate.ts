/**
 * The `${token}` grammar a JSON field value may carry, and the strict formatter
 * built on it.
 *
 * Lives outside JsonEditor.tsx so that file exports only its component, the
 * same constraint pages/settings/branchDiffFanout.ts was extracted for.
 */
import { getErrorMessage } from '@/lib/utils'

const TEMPLATE_TOKEN_PATTERN = /\$\{([^}]*)\}/g

/**
 * A bare word that `jsonRelaxed` may read as a variable reference, and the only
 * thing this file still spells as an identifier.
 *
 * Kept narrow on purpose: relaxed JSON turns unquoted text into a reference when
 * it looks like a name, so widening this to the token grammar below would make
 * almost any bare text a variable. Exported for `jsonRelaxed.classifyBare`,
 * which is its only caller.
 */
export const BARE_WORD_VARIABLE_PATTERN = /^[A-Za-z_][A-Za-z0-9_.-]*$/

// The token body the BACKEND accepts, character for character:
// `event_service._JSON_TEMPLATE_TOKEN_NAME_PATTERN` is `^[^"\\}\x00-\x1f]+$`.
// A scan legitimately writes `${property.Москва}` or `${property.Albany, OR}` —
// `derive_display_name` falls back to the raw JSON path whenever it cannot
// sanitise one — and the form re-sends every field value on save, so an
// identifier grammar here blocked every later edit of such an event, including
// one that only touched the description. The backend accepted
// those saves from this batch on; this file was the remaining door that did not.
// What stays out is what a JSON string cannot hold verbatim (a quote, a
// backslash, a C0 control character) plus `}`, which ends the token.
// eslint-disable-next-line no-control-regex -- the C0 range is excluded, never matched: a JSON string cannot hold one verbatim and the backend pattern above refuses them too
const JSON_TEMPLATE_TOKEN_NAME_PATTERN = /^[^"\\}\x00-\x1f]+$/
// eslint-disable-next-line no-control-regex -- same C0 exclusion as the token name pattern above
const JSON_TEMPLATE_VALUE_PATTERN = /"\$\{[^"\\}\x00-\x1f]+\}"|\$\{[^"\\}\x00-\x1f]+\}/g
// eslint-disable-next-line no-control-regex -- same C0 exclusion as the token name pattern above
const JSON_TEMPLATE_KEY_PATTERN = /"\$\{[^"\\}\x00-\x1f]+\}"\s*:/

const SENTINEL_BASE = '__TRIPL_VAR_'

export function templateJsonError(text: string): string | null {
  const tokens = [...text.matchAll(TEMPLATE_TOKEN_PATTERN)].map(match => match[1] ?? '')
  if (tokens.some(token => !JSON_TEMPLATE_TOKEN_NAME_PATTERN.test(token))) {
    return 'A ${...} token must name a property and cannot contain a quote, a backslash or a control character.'
  }
  const templateValues = text.match(JSON_TEMPLATE_VALUE_PATTERN) ?? []
  if (templateValues.length !== tokens.length) {
    return 'Property templates must occupy a complete JSON value.'
  }
  if (JSON_TEMPLATE_KEY_PATTERN.test(text)) {
    return 'Property templates cannot be JSON object keys.'
  }
  return null
}

export function validateJsonWithVars(text: string): string | null {
  if (!text.trim()) return null
  const templateError = templateJsonError(text)
  if (templateError) return templateError
  if (!text.includes('${')) {
    try { JSON.parse(text); return null } catch (e) { return getErrorMessage(e) }
  }
  // Replace ${var} placeholders with a sentinel string before validating, so
  // partially-templated JSON parses successfully. Quoted tokens ("${var}")
  // must be swapped together with their quotes or the sentinel doubles them.
  const safe = text.replace(JSON_TEMPLATE_VALUE_PATTERN, '"__var__"')
  try { JSON.parse(safe); return null } catch (e) { return getErrorMessage(e) }
}

/**
 * Re-indent `text` as JSON, carrying any ${var} placeholders through untouched.
 *
 * Returns null when the text is not valid JSON, so every caller decides for
 * itself whether that is worth reporting. Placeholders are stashed behind
 * sentinels before parsing — a bare ${token} is a syntax error to JSON.parse,
 * and a quoted one would come back escaped from JSON.stringify.
 *
 * Each occurrence gets its own numbered sentinel, because the restore replaces
 * a string needle and that only ever swaps the first match.
 */
export function formatJsonTemplate(text: string): string | null {
  if (!text.trim()) return null
  if (templateJsonError(text)) return null
  if (!text.includes('${')) {
    try { return JSON.stringify(JSON.parse(text), null, 2) } catch { return null }
  }

  // A sentinel has to survive the round trip unambiguously, and the input is
  // not enough to check against: a \u005f escape only becomes an underscore
  // after parsing, so a literal can collide with a sentinel that was unique in
  // the source. Lengthen the prefix until every sentinel appears exactly once
  // in the formatted output.
  let prefix = SENTINEL_BASE
  for (let attempt = 0; attempt < 8; attempt++) {
    const stashPrefix = prefix
    const placeholders = new Map<string, string>()
    const safe = text.replace(JSON_TEMPLATE_VALUE_PATTERN, match => {
      const sentinel = `${stashPrefix}${placeholders.size}__`
      placeholders.set(sentinel, match)
      return `"${sentinel}"`
    })

    let formatted: string
    try { formatted = JSON.stringify(JSON.parse(safe), null, 2) } catch { return null }

    const needles = [...placeholders.keys()].map(sentinel => `"${sentinel}"`)
    if (needles.some(needle => formatted.split(needle).length !== 2)) {
      prefix = `_${prefix}`
      continue
    }
    placeholders.forEach((placeholder, sentinel) => {
      formatted = formatted.replace(`"${sentinel}"`, placeholder)
    })
    return formatted
  }
  return null
}
