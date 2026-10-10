import { api, withBranch } from './client'

export interface AiStatus {
  enabled: boolean
}

export interface AiFieldSuggestion {
  field_name: string
  description: string
}

export interface AiDescribeResponse {
  description: string
  field_suggestions: AiFieldSuggestion[]
}

export interface AiAskSource {
  title: string
  entity_type: string
  route_path: string
}

export interface AiAskResponse {
  answer: string
  sources: AiAskSource[]
  semantic_used: boolean
}

export const aiApi = {
  status: (slug: string) =>
    api.get<AiStatus>(`/projects/${slug}/ai/status`),

  describeEvent: (slug: string, eventId: string, branchId?: string | null) =>
    api.post<AiDescribeResponse>(
      withBranch(`/projects/${slug}/ai/describe-event`, branchId),
      { event_id: eventId },
    ),

  ask: (slug: string, question: string, branchId?: string | null) =>
    api.post<AiAskResponse>(
      withBranch(`/projects/${slug}/ai/ask`, branchId),
      { question },
    ),
}
