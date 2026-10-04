import type { EventFieldVariableValue } from './events'

/**
 * Kinds of entity the search index holds.
 *
 * This is a HAND-WRITTEN copy of the backend's `SearchEntityType` literal, kept
 * in step by hand — `api.gen.ts` carries the generated one, but the app imports
 * this. Adding a value on the backend without adding it here compiles fine and
 * simply loses the new kind at runtime — nothing on this side notices.
 *
 * There are THREE hand-written copies of the list, and only one of them is
 * guarded:
 *
 * - this file, unguarded;
 * - `SEARCH_TYPE_META` in command-palette.tsx, a `Record<SearchEntityType, …>`
 *   and so fails the build for a value present HERE and missing there — it
 *   catches the second half of the edit, never the first;
 * - `tripl_cli.api.search.ENTITY_TYPES`, which `cli/tests/test_contract.py`
 *   compares against the OpenAPI enum. That is the only check that fails when
 *   the BACKEND gains a value and a copy does not.
 *
 * So a backend addition is caught by the CLI's contract test, in CI, and not by
 * anything in this package. Extend all three.
 */
export type SearchEntityType =
  | 'event'
  | 'event_type'
  | 'field'
  | 'meta_field'
  | 'variable'
  | 'relation'
  | 'tag'
  | 'metric'
  | 'fact_table'
  | 'scan_config'
  | 'alert_rule'
  | 'doc'

export interface SearchEventVariableValue extends EventFieldVariableValue {
  field_definition_id: string
  field_name: string
  field_display_name: string
}

/**
 * One folded member of a {@link SearchVariantGroup} (#238 JR-20). Slim on
 * purpose: enough to name and open the event.
 */
export interface SearchVariant {
  id: string
  entity_id: string
  event_id: string | null
  title: string
  /** The value this member substituted for the group's placeholder. */
  value: string
  route_path: string
  score: number
  confidence: number
}

/**
 * Event hits folded under their best-ranked member (#238 JR-20): events of one
 * event type whose names differ only in the value of ONE naming-rule
 * placeholder. Only present when the search asked for `group_variants`.
 */
export interface SearchVariantGroup {
  /** Stable across searches: the event type plus `pattern`. */
  key: string
  /** The name with the varying value replaced by `{placeholder}`. */
  pattern: string
  placeholder: string
  /**
   * Every member INCLUDING the representative: `count - 1 === variants.length`.
   * Folded from the retrieval window only, so a lower bound when the response
   * is `truncated`.
   */
  count: number
  /** The other members, best-ranked first. */
  variants: SearchVariant[]
}

export interface SearchResult {
  id: string
  entity_type: SearchEntityType
  entity_id: string
  parent_event_id: string | null
  event_id: string | null
  name: string | null
  /** Legacy field from search index — may be null for non-event results */
  implemented: boolean | null
  variable_values: SearchEventVariableValue[]
  title: string
  subtitle: string
  description: string
  snippet: string
  route_path: string
  score: number
  /**
   * How certain this result is, in [0, 1] — an ABSOLUTE property of the result,
   * NOT a fraction of the response's top hit. Comparable across two
   * different searches: a query that matched nothing well comes back low on
   * every item instead of being 1.0 by construction. See
   * `schemas/search.py SearchResult.confidence` and
   * `_search_query.finalize_results` for how it is derived.
   */
  confidence: number
  highlights: string[]
  /**
   * Whether the MEANING LEG is why this row is here — the keyword leg's own
   * ranked candidate window did not hold it.
   *
   * NOT "no keyword matched this": the keyword leg is itself a capped scan, so a
   * weak match (a stem-only one, which earns no exact-match tier) can be
   * displaced from that window and re-enter through the vector leg, reading
   * `true` on a row the keyword index did match. The palette's chip is worded
   * "the keyword ranking didn't surface this" for exactly that reason.
   *
   * Narrower than `SearchResponse.semantic_used`, which says only that the leg
   * ran. A hit the keyword ladder produced reads `false` even when the vector
   * leg also ranked it and even when its `confidence` comes from that leg's
   * cosine — the vector leg is a top-k scan that returns rows for any query, so
   * "it was in that window too" says nothing about this row.
   */
  semantic_used: boolean
  /**
   * Set on the representative of a folded variant group, and only when the
   * search asked for `group_variants`; `null` otherwise.
   */
  variant_group?: SearchVariantGroup | null
}

export interface SearchResponse {
  items: SearchResult[]
  /**
   * Hits IN THIS RESPONSE, not a catalog-wide count — `/search` takes no
   * `offset` and cannot be paged. Read `truncated`, not `total`, to learn
   * whether anything was dropped. With `group_variants` it and
   * `limit` count ROWS: a folded group is one, so its members never eat into
   * the page (#238 JR-20).
   */
  total: number
  /** Ranked hits exist that this response does not carry; raise `limit`. */
  truncated: boolean
  /** Whether the semantic leg ran at all. See `SearchResult.semantic_used`. */
  semantic_used: boolean
}
