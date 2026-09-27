import { api } from './client'
import type { IncidentSummaryResponse } from '../types'

export type {
  IncidentSummaryBody,
  IncidentSummaryDisabledReason,
  IncidentSummaryFact,
  IncidentSummaryFactKind,
  IncidentSummaryResponse,
  IncidentSummarySentence,
  IncidentSummarySentenceRole,
  IncidentSummaryState,
} from '../types'

function summaryPath(slug: string, correlationGroupId: string): string {
  return `/projects/${slug}/alert-inbox/${encodeURIComponent(correlationGroupId)}/summary`
}

/**
 * Incident summary (F14, #267). `get` never calls the LLM; `ensure` generates
 * only when the facts changed; `regenerate` (editors) always generates. All
 * three answer 200 with a `state` for AI-off and provider failures.
 */
export const incidentSummaryApi = {
  get: (slug: string, correlationGroupId: string) =>
    api.get<IncidentSummaryResponse>(summaryPath(slug, correlationGroupId)),

  ensure: (slug: string, correlationGroupId: string) =>
    api.post<IncidentSummaryResponse>(summaryPath(slug, correlationGroupId)),

  regenerate: (slug: string, correlationGroupId: string) =>
    api.post<IncidentSummaryResponse>(`${summaryPath(slug, correlationGroupId)}/regenerate`),
}
