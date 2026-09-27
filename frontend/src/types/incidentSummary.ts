/**
 * Incident summary (F14, #267): a short, cited account of one Alerting Inbox
 * incident, written by the instance's LLM from a numbered list of facts.
 *
 * Hand-written from `backend/src/tripl/schemas/incident_summary.py` until those
 * schemas land in the committed OpenAPI (`api.gen.ts`); switch each alias to
 * `components['schemas'][...]` once they do.
 */

export type IncidentSummaryFactKind =
  | 'incident'
  | 'scope'
  | 'attribution'
  | 'release'
  | 'similar'
  | 'note'
  | 'comment'

/** One numbered fact. `id` is what a sentence's `[n]` refers to. */
export interface IncidentSummaryFact {
  /** 1-based. */
  id: number
  kind: IncidentSummaryFactKind
  /** Already redacted: no sensitive field values, no drift sample values. */
  text: string
  /** In-app route (incident, scope drilldown or event discussion), or null. */
  href: string | null
}

export type IncidentSummarySentenceRole =
  | 'what_broke'
  | 'cause'
  | 'release'
  | 'history'
  | 'discussion'

export interface IncidentSummarySentence {
  /** No inline `[n]` markers; citations come from `fact_ids`. */
  text: string
  role: IncidentSummarySentenceRole
  /** Non-empty subset of the body's fact ids, except for the backend's
   *  "The cause is unknown ..." sentence. */
  fact_ids: number[]
  /** False for the fixed, non-LLM unknown-cause sentence. */
  generated: boolean
}

export interface IncidentSummaryBody {
  sentences: IncidentSummarySentence[]
  /** The facts this body was generated from; citations resolve here. */
  facts: IncidentSummaryFact[]
  cause_known: boolean
  facts_hash: string
  generated_at: string
}

export type IncidentSummaryState = 'disabled' | 'missing' | 'stale' | 'ready' | 'failed'

export type IncidentSummaryDisabledReason = 'ai_off' | 'demo'

export interface IncidentSummaryResponse {
  correlation_group_id: string
  state: IncidentSummaryState
  disabled_reason: IncidentSummaryDisabledReason | null
  /** Null when disabled. */
  current_facts_hash: string | null
  /** Set for ready and stale (stale = the previous body), null otherwise. */
  summary: IncidentSummaryBody | null
}
