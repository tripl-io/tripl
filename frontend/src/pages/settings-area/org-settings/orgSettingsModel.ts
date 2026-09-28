import type {
  OrgSettings,
  OrgSettingSource,
  OrgSettingsUpdate,
  OrgSettingsValues,
} from '@/api/orgSettings'

/**
 * The pure half of Organization › Email, AI and Limits (F20 PR9): the draft,
 * what a save sends, and the rules the backend applies that the page has to
 * say out loud (credential groups, operator ceilings, the fallback policy).
 * Mirrors backend/src/tripl/services/_org_settings_merge.py.
 */

export type OrgSection = 'limits' | 'email' | 'ai'

export const ORG_SECTIONS: readonly OrgSection[] = ['email', 'ai', 'limits']

/** The settings path of each section (`/settings/<path>`). */
export const ORG_SECTION_PATHS: Record<OrgSection, string> = {
  email: 'organization/email',
  ai: 'organization/ai',
  limits: 'organization/limits',
}

export function orgSectionForPath(path: string): OrgSection | null {
  const found = ORG_SECTIONS.find(section => ORG_SECTION_PATHS[section] === path)
  return found ?? null
}

/**
 * One edited field. A string or boolean is the organization's new value;
 * `null` clears the organization's own value, so the field inherits again.
 * Numbers stay the text typed until the save, so an emptied field stays empty.
 */
export type DraftValue = string | boolean | null
export type OrgDraft = Readonly<Record<string, DraftValue>>
export type OrgDrafts = Readonly<Record<OrgSection, OrgDraft>>

export const EMPTY_DRAFTS: OrgDrafts = { limits: {}, email: {}, ai: {} }

/** Secrets are write-only: the response only says whether one is configured. */
export const SECRET_FIELDS: ReadonlySet<string> = new Set(['ai_api_key', 'smtp_password'])

/** Sent as numbers; typed as text. */
export const NUMBER_FIELDS: ReadonlySet<string> = new Set([
  'scan_row_limit_default',
  'metrics_row_limit_default',
  'smtp_port',
  'ai_timeout_seconds',
  'ai_max_output_tokens',
])

/** Capped at the operator's value (backend `CEILING_FIELDS`). */
export type CeilingField = keyof OrgSettings['ceilings']
export const CEILING_FIELDS: readonly CeilingField[] = [
  'scan_row_limit_default',
  'metrics_row_limit_default',
  'ai_timeout_seconds',
  'ai_max_output_tokens',
]

function isCeilingField(field: string): field is CeilingField {
  return (CEILING_FIELDS as readonly string[]).includes(field)
}

/**
 * The credential groups (backend `CREDENTIAL_GROUPS`, critique #13). An
 * endpoint and the credential sent to it are one unit: once an organization
 * sets any member, the whole group is its own, an unset member takes the
 * built-in default (a secret: none), and the operator's key or password is
 * never sent to the organization's server.
 */
export const AI_ENDPOINT_GROUP = ['ai_base_url', 'ai_model', 'ai_api_key'] as const
export const SMTP_GROUP = [
  'smtp_host',
  'smtp_port',
  'smtp_security',
  'smtp_username',
  'smtp_password',
  'smtp_from_address',
] as const

export function sourceOf(settings: OrgSettings, section: OrgSection, field: string): OrgSettingSource {
  return settings.sources[`${section}.${field}`] ?? 'default'
}

type SectionValues = Record<string, string | number | boolean>

function sectionValues(values: OrgSettingsValues, section: OrgSection): SectionValues {
  return values[section] as unknown as SectionValues
}

/** The value in effect for the organization now (secrets: never sent). */
export function savedValue(settings: OrgSettings, section: OrgSection, field: string): string | number | boolean {
  return sectionValues(settings, section)[field] ?? ''
}

/** What the organization would run with if it cleared its own value. */
export function inheritedValue(settings: OrgSettings, section: OrgSection, field: string): string | number | boolean {
  return sectionValues(settings.inherited, section)[field] ?? ''
}

/** Whether the field holds a value of the organization's own (not the operator scope). */
export function isOwnValue(settings: OrgSettings, section: OrgSection, field: string): boolean {
  return settings.scope === 'organization' && sourceOf(settings, section, field) === 'org'
}

/** What the input shows: the draft, else the inherited value after a clear, else the saved one. */
export function displayValue(
  settings: OrgSettings,
  draft: OrgDraft,
  section: OrgSection,
  field: string,
): string | number | boolean {
  if (field in draft) {
    const value = draft[field]
    return value === null ? inheritedValue(settings, section, field) : (value as string | boolean)
  }
  if (SECRET_FIELDS.has(field)) return ''
  return savedValue(settings, section, field)
}

/**
 * Put one edit in the draft. Typing back the saved value drops the edit, so
 * Save does not light up for a field that is what it was.
 */
export function withEdit(
  settings: OrgSettings,
  draft: OrgDraft,
  section: OrgSection,
  field: string,
  value: DraftValue,
): OrgDraft {
  const next: Record<string, DraftValue> = { ...draft }
  const unchanged =
    value !== null &&
    (SECRET_FIELDS.has(field)
      ? value === ''
      : String(value) === String(savedValue(settings, section, field)))
  if (unchanged) delete next[field]
  else next[field] = value
  return next
}

/** A number field's problem, or null. `ceiling` is the operator's maximum. */
export function numberError(field: string, value: DraftValue, settings: OrgSettings): string | null {
  if (value === null || typeof value === 'boolean') return null
  const text = value.trim()
  if (!/^\d+$/.test(text) || Number(text) < 1) return 'Enter a whole number of at least 1.'
  if (field === 'smtp_port' && Number(text) > 65535) return 'A port is at most 65535.'
  // The operator's own scope has no ceiling above it.
  if (settings.scope === 'organization' && isCeilingField(field)) {
    const ceiling = settings.ceilings[field]
    if (Number(text) > ceiling) return `The operator's maximum is ${ceiling.toLocaleString('en-US')}.`
  }
  return null
}

export function draftInvalid(settings: OrgSettings, draft: OrgDraft): boolean {
  return Object.entries(draft).some(
    ([field, value]) => NUMBER_FIELDS.has(field) && numberError(field, value, settings) !== null,
  )
}

/** The PATCH body for one section's draft. An empty secret is no change. */
export function buildOrgUpdate(section: OrgSection, draft: OrgDraft): OrgSettingsUpdate {
  const fields: Record<string, string | number | boolean | null> = {}
  for (const [field, value] of Object.entries(draft)) {
    if (value === null) {
      fields[field] = null
    } else if (SECRET_FIELDS.has(field)) {
      if (typeof value === 'string' && value.trim()) fields[field] = value
    } else if (NUMBER_FIELDS.has(field) && typeof value === 'string') {
      fields[field] = Number(value.trim())
    } else {
      fields[field] = value
    }
  }
  return { [section]: fields } as OrgSettingsUpdate
}

export function hasChanges(update: OrgSettingsUpdate): boolean {
  return Object.values(update).some(
    fields => fields !== null && fields !== undefined && Object.keys(fields).length > 0,
  )
}

/** Whether the organization owns the group now (any member is its own). */
export function groupOwned(settings: OrgSettings, section: OrgSection, group: readonly string[]): boolean {
  return group.some(field => isOwnValue(settings, section, field))
}

/** Whether it will own the group once this draft is saved. */
export function groupOwnedAfterSave(
  settings: OrgSettings,
  draft: OrgDraft,
  section: OrgSection,
  group: readonly string[],
): boolean {
  if (settings.scope === 'operator') return false
  return group.some(field => {
    if (!(field in draft)) return isOwnValue(settings, section, field)
    const value = draft[field]
    if (value === null) return false
    return SECRET_FIELDS.has(field) ? typeof value === 'string' && value.trim() !== '' : true
  })
}

/** Whether the organization will hold a secret of its own after this save. */
export function ownSecretAfterSave(
  settings: OrgSettings,
  draft: OrgDraft,
  section: OrgSection,
  field: string,
): boolean {
  if (field in draft) {
    const value = draft[field]
    if (value === null) return false
    if (typeof value === 'string' && value.trim() !== '') return true
  }
  return isOwnValue(settings, section, field)
}

/** A draft that clears every member of the group: back to the operator's. */
export function clearGroup(draft: OrgDraft, settings: OrgSettings, section: OrgSection, group: readonly string[]): OrgDraft {
  const next: Record<string, DraftValue> = { ...draft }
  for (const field of group) {
    if (isOwnValue(settings, section, field)) next[field] = null
    else delete next[field]
  }
  return next
}

/**
 * What the group notes say. `null` when there is nothing to warn about.
 *
 * `starting`: the draft makes an inherited group the organization's own, so
 * every member it leaves alone drops to the built-in default on save.
 * `missingSecret`: the group will be the organization's without a key or
 * password of its own, and the operator's is never sent to it.
 */
export function groupWarning(
  settings: OrgSettings,
  draft: OrgDraft,
  section: 'ai' | 'email',
): { starting: boolean; missingSecret: boolean } | null {
  const group = section === 'ai' ? AI_ENDPOINT_GROUP : SMTP_GROUP
  const secret = section === 'ai' ? 'ai_api_key' : 'smtp_password'
  if (!groupOwnedAfterSave(settings, draft, section, group)) return null
  const starting = !groupOwned(settings, section, group)
  const missingSecret = !ownSecretAfterSave(settings, draft, section, secret)
  if (!starting && !missingSecret) return null
  return { starting, missingSecret }
}

export const SECTION_TITLES: Record<OrgSection, string> = {
  email: 'Email',
  ai: 'AI',
  limits: 'Limits',
}
