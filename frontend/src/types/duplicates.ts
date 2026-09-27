/**
 * Duplicate detection and naming-convention lint (F12, #265).
 *
 * Hand-written from `backend/src/tripl/schemas/duplicates.py` until those
 * schemas land in the committed OpenAPI (`api.gen.ts`); switch each alias to
 * `components['schemas'][...]` once they do. No LLM is involved: the score combines lexical similarity
 * (token Jaccard / character trigrams) with the search embeddings when the
 * instance has them.
 */
import type { EventStatus } from './events'

/** One would-be event, as the create form, the bulk paste or the shadow inbox
 *  describes it. */
export interface DuplicateCandidate {
  name: string
  event_type_id: string
  description?: string
  /** The values that would be written, in the create payload's own shape.
   *  Under a naming rule the server renders the name from these. */
  field_values?: { field_definition_id: string; value: string }[]
  /** The event being edited, so it is never reported as its own duplicate. */
  event_id?: string
}

/** An existing event the candidate looks like. */
export interface DuplicateMatch {
  event_id: string
  name: string
  /** Combined score, 0..1. Only matches at or above the threshold are sent. */
  score: number
  /** Why it matched, in words ("similar name", "same field values"). */
  reasons: string[]
  event_type_id: string
  status: EventStatus
}

export type NameLintCode = 'case' | 'separator' | 'verb_order' | 'prefix'

/** One way the candidate's name departs from the project's convention. */
export interface NameLintIssue {
  code: NameLintCode
  message: string
  /** The name rewritten to follow the convention on this point. */
  suggestion: string
}

/** The convention the server inferred from the type's live events. */
export interface NamingConvention {
  case: 'snake' | 'camel' | 'pascal' | 'kebab' | 'space' | 'mixed'
  space_style?: 'title' | 'lower' | 'sentence' | null
  separator?: string | null
  verb_position: 'first' | 'last' | 'unknown'
  prefix?: string | null
  confidence: number
  sample_size: number
}

/** The answer for one candidate, in the order the candidates were sent. */
export interface DuplicateCheckResult {
  /** The name that was checked: the candidate's own, or the one its naming
   *  rule renders from the field values. */
  name?: string
  /** Top matches (at most three), best first. */
  duplicates: DuplicateMatch[]
  lint: NameLintIssue[]
  /** A name that fixes every lint issue at once; null when there is none. */
  suggestion?: string | null
  /** False under a naming rule: the rule decides the spelling, nothing is linted. */
  lint_applicable?: boolean
  convention?: NamingConvention | null
}

/** `POST /projects/{slug}/events/duplicate-check`. */
export interface DuplicateCheckResponse {
  /** One per candidate, in the order sent. */
  items: DuplicateCheckResult[]
  threshold: number
  /** Whether an embedding cosine took part; otherwise the scores are lexical. */
  semantic_used?: boolean
}

/** One member of a duplicate cluster. */
export interface DuplicateClusterEvent {
  id: string
  name: string
  status: EventStatus
  /** Occurrences over the last seven days; 0 when never collected. */
  volume_7d: number
  event_type_id: string
}

/** Events that look alike pairwise at or above the threshold. */
export interface DuplicateCluster {
  events: DuplicateClusterEvent[]
  /** The strongest pairwise score inside the cluster, 0..1. */
  score: number
}

/** `GET /projects/{slug}/duplicates?cursor=`. */
export interface DuplicateClustersResponse {
  items: DuplicateCluster[]
  /** Opaque; passed back as `?cursor=`. Null on the last page. */
  next_cursor: string | null
  total: number
  threshold: number
  /** The catalog was larger than the cap the clustering reads. */
  truncated?: boolean
}

/** `POST /projects/{slug}/duplicates/dismiss`. */
export interface DuplicateDismissBody {
  event_a_id: string
  event_b_id: string
}

export interface DuplicateDismissResponse extends DuplicateDismissBody {
  /** False when the pair had already been dismissed. */
  created: boolean
}
