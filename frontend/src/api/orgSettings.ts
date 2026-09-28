import { api } from './client'
import type { components } from '../types/api.gen'
import type { SettingsTestResponse } from '../types'

/**
 * An organization's own settings (F20 PR9, backend/src/tripl/api/v1/org_settings.py):
 * mail, AI chat and the row-limit defaults, each resolved organization value ->
 * operator value -> environment. `/orgs/...` paths are never rewritten by the
 * client: the organization is named in the path.
 */
export type OrgSettings = components['schemas']['OrgSettingsResponse']
export type OrgSettingsUpdate = components['schemas']['OrgSettingsUpdate']
export type OrgSettingsValues = components['schemas']['OrgSettingsValues']
/** Where one of an organization's values came from. */
export type OrgSettingSource = OrgSettings['sources'][string]

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
}
