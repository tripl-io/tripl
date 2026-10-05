import { api } from './client'
import { currentOrgSlug } from '@/lib/activeOrg'
import type { components } from '@/types/api.gen'
import type {
  ServiceSettings,
  ServiceSettingsUpdate,
  SettingsTestResponse,
} from '@/types'

/** The built-in AI system prompts, for "Restore default". */
export type AiPromptDefaults = components['schemas']['AiPromptDefaultsResponse']

/** The opt-in usage ping: on or off, where to, and exactly what it last sent. */
export type TelemetryStatus = components['schemas']['TelemetryStatusResponse']

/** The row caps a scan falls back to, readable by any member of the organization. */
export type RowLimitDefaults = components['schemas']['RowLimitDefaultsResponse']

/**
 * The Platform settings (F20 PR9): the operator scope, platform admins only.
 * Its email and AI values are the defaults every organization without its own
 * inherits, and the relay account mail (sign-up, password reset, invitations)
 * always uses. An organization's own values live in api/orgSettings.ts.
 */
export const serviceSettingsApi = {
  get: () => api.get<ServiceSettings>('/platform/settings'),
  aiPromptDefaults: () => api.get<AiPromptDefaults>('/settings/ai/defaults'),
  // The ACTIVE organization's caps (F20 PR9: an organization may lower them).
  // The legacy route guesses an organization for a user in several, so the
  // one on screen is named whenever there is one.
  rowLimitDefaults: () => {
    const org = currentOrgSlug()
    return api.get<RowLimitDefaults>(
      org ? `/orgs/${encodeURIComponent(org)}/settings/row-limits` : '/settings/row-limits',
    )
  },
  update: (data: ServiceSettingsUpdate) => api.patch<ServiceSettings>('/platform/settings', data),
  telemetry: () => api.get<TelemetryStatus>('/platform/settings/telemetry'),
  testAi: (prompt?: string) =>
    api.post<SettingsTestResponse>('/platform/settings/ai/test', prompt ? { prompt } : {}),
  // Omitting the recipient mails the signed-in owner — the address they are
  // most likely to be able to check, and one fewer field before the first probe.
  testEmail: (recipient?: string) =>
    api.post<SettingsTestResponse>('/platform/settings/email/test', recipient ? { recipient } : {}),
}
