import { api } from './client'
import type {
  CheckRunDirectoryList,
  CheckRunList,
  CheckRunSettings,
  CheckRunSettingsUpdate,
  EventCodeUsage,
} from '../types'

/** What the check history is narrowed to; every field optional. */
export interface CheckRunListParams {
  repository?: string
  checkRoot?: string
  limit?: number
  offset?: number
}

export const checkRunsApi = {
  /** `GET /check-runs`: the uploaded runs, newest first, each as a summary. */
  list: (slug: string, params: CheckRunListParams = {}, signal?: AbortSignal) => {
    const sp = new URLSearchParams()
    if (params.repository) sp.set('repository', params.repository)
    if (params.checkRoot) sp.set('check_root', params.checkRoot)
    if (params.limit !== undefined) sp.set('limit', String(params.limit))
    if (params.offset !== undefined) sp.set('offset', String(params.offset))
    const qs = sp.toString()
    return api.get<CheckRunList>(`/projects/${slug}/check-runs${qs ? `?${qs}` : ''}`, signal)
  },

  /** The repositories and check directories with runs kept, for the history's filters. */
  directories: (slug: string, signal?: AbortSignal) =>
    api.get<CheckRunDirectoryList>(`/projects/${slug}/check-runs/directories`, signal),

  /** "Used in code": where each repository directory's current run sends the event. */
  codeUsage: (slug: string, eventId: string, signal?: AbortSignal) =>
    api.get<EventCodeUsage>(
      `/projects/${slug}/events/${encodeURIComponent(eventId)}/code-usage`,
      signal,
    ),

  getSettings: (slug: string, signal?: AbortSignal) =>
    api.get<CheckRunSettings>(`/projects/${slug}/check-run-settings`, signal),

  updateSettings: (slug: string, data: CheckRunSettingsUpdate) =>
    api.patch<CheckRunSettings>(`/projects/${slug}/check-run-settings`, data),
}
