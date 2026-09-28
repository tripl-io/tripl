import { api } from './client'
import type { components } from '../types/api.gen'
import type { SettingsTestResponse } from '../types'

/**
 * An organization's own settings (F20 PR9, PR10, PR12,
 * backend/src/tripl/api/v1/org_settings.py): mail, AI chat, search embeddings
 * and the row-limit defaults, each resolved organization value -> operator
 * value -> environment; and the issue-tracker defaults its projects inherit. `/orgs/...` paths are never rewritten by the
 * client: the organization is named in the path.
 */
export type OrgSettings = components['schemas']['OrgSettingsResponse']
export type OrgSettingsUpdate = components['schemas']['OrgSettingsUpdate']
export type OrgSettingsValues = components['schemas']['OrgSettingsValues']
/** Where one of an organization's values came from. */
export type OrgSettingSource = OrgSettings['sources'][string]
/** The Jira/Linear defaults every project of the organization falls back to. */
export type OrgTrackerDefaults = components['schemas']['OrgTrackerDefaultsResponse']
export type OrgTrackerDefaultsUpdate = components['schemas']['OrgTrackerDefaultsUpdate']

function base(org: string): string {
  return `/orgs/${encodeURIComponent(org)}/settings`
}

export const orgSettingsApi = {
  get: (org: string) => api.get<OrgSettings>(base(org)),
  /** Sparse: a field left out is untouched, `null` clears the organization's value. */
  update: (org: string, data: OrgSettingsUpdate) => api.patch<OrgSettings>(base(org), data),
  testAi: (org: string, prompt?: string) =>
    api.post<SettingsTestResponse>(`${base(org)}/ai/test`, prompt ? { prompt } : {}),
  /** Omitting the recipient mails the signed-in admin. */
  testEmail: (org: string, recipient?: string) =>
    api.post<SettingsTestResponse>(`${base(org)}/email/test`, recipient ? { recipient } : {}),
  /** Owner/admin only; secrets come back as `*_configured`, never the value. */
  getTrackers: (org: string) => api.get<OrgTrackerDefaults>(`${base(org)}/trackers`),
  /** Sparse: an omitted field is unchanged, `null` or `""` clears it. */
  updateTrackers: (org: string, data: OrgTrackerDefaultsUpdate) =>
    api.patch<OrgTrackerDefaults>(`${base(org)}/trackers`, data),
}
