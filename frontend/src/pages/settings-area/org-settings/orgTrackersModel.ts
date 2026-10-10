import type { OrgSettingSource, OrgTrackerDefaults, OrgTrackerDefaultsUpdate } from '@/api/orgSettings'

/**
 * The pure half of Organization › Trackers (F20 PR12): the Jira and Linear
 * defaults every project of the organization falls back to for the fields its
 * own tracker config leaves empty. Mirrors
 * backend/src/tripl/services/org_tracker_defaults_service.py.
 *
 * A field is keyed `<tracker>.<field>` (`jira.base_url`), as the response's
 * `sources` are. In the draft a string is the new value and `null` clears the
 * organization's value; a secret is write-only, so an empty one is no change.
 */

export const ORG_TRACKERS_PATH = 'organization/trackers'

export type TrackerKind = 'jira' | 'linear'
export type TrackerDefaultKey =
  | 'jira.base_url'
  | 'jira.auth_email'
  | 'jira.api_token'
  | 'jira.project_key'
  | 'linear.api_key'
  | 'linear.team_id'

export type TrackerDraft = Readonly<Partial<Record<TrackerDefaultKey, string | null>>>

export const TRACKER_SECRET_KEYS: ReadonlySet<TrackerDefaultKey> = new Set<TrackerDefaultKey>([
  'jira.api_token',
  'linear.api_key',
])

function split(key: TrackerDefaultKey): [TrackerKind, string] {
  const [tracker, field] = key.split('.') as [TrackerKind, string]
  return [tracker, field]
}

/** Whether the organization has its own value for the field. */
export function trackerSource(defaults: OrgTrackerDefaults, key: TrackerDefaultKey): OrgSettingSource {
  return defaults.sources[key] === 'org' ? 'org' : 'default'
}

/** The saved value of a non-secret field ("" when the organization has none). */
export function savedTrackerValue(defaults: OrgTrackerDefaults, key: TrackerDefaultKey): string {
  if (TRACKER_SECRET_KEYS.has(key)) return ''
  const [tracker, field] = split(key)
  const values = defaults[tracker] as unknown as Record<string, string | boolean>
  const value = values[field]
  return typeof value === 'string' ? value : ''
}

/** Whether a secret is stored for the organization (never its value). */
export function secretConfigured(defaults: OrgTrackerDefaults, key: TrackerDefaultKey): boolean {
  if (key === 'jira.api_token') return defaults.jira.api_token_configured
  if (key === 'linear.api_key') return defaults.linear.api_key_configured
  return false
}

/** What the input shows: the draft, else the saved value (a secret: blank). */
export function trackerDisplayValue(
  defaults: OrgTrackerDefaults,
  draft: TrackerDraft,
  key: TrackerDefaultKey,
): string {
  if (key in draft) return draft[key] ?? ''
  return savedTrackerValue(defaults, key)
}

/**
 * One edit. Emptying a text field the organization has a value for clears it
 * (`null`); typing the saved value back, or leaving a secret empty, drops the
 * edit so Save does not light up for nothing.
 */
export function withTrackerEdit(
  defaults: OrgTrackerDefaults,
  draft: TrackerDraft,
  key: TrackerDefaultKey,
  value: string | null,
): TrackerDraft {
  const next: Partial<Record<TrackerDefaultKey, string | null>> = { ...draft }
  const secret = TRACKER_SECRET_KEYS.has(key)
  const own = trackerSource(defaults, key) === 'org'
  let normalized: string | null = value
  if (!secret && value !== null && value.trim() === '') normalized = own ? null : ''
  const unchanged =
    normalized === null
      ? !own
      : secret
        ? normalized.trim() === ''
        : normalized === savedTrackerValue(defaults, key)
  if (unchanged) delete next[key]
  else next[key] = normalized
  return next
}

const JIRA_PROJECT_KEY_RE = /^[A-Z][A-Z0-9_]{1,31}$/
const LINEAR_ID_RE = /^[A-Za-z0-9_-]{1,64}$/
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

/**
 * A filled field's format problem, or null (backend `alerting_validation`).
 * The private-address check of the Jira site is the server's: it resolves DNS.
 */
export function trackerFieldError(key: TrackerDefaultKey, value: string | null | undefined): string | null {
  if (value === null || value === undefined) return null
  const text = value.trim()
  if (text === '') return null
  switch (key) {
    case 'jira.base_url': {
      let parsed: URL | null
      try {
        parsed = /\s/.test(text) ? null : new URL(text)
      } catch {
        parsed = null
      }
      return parsed && parsed.protocol === 'https:' && parsed.hostname
        ? null
        : 'Enter an https URL, such as https://acme.atlassian.net.'
    }
    case 'jira.auth_email':
      return EMAIL_RE.test(text) ? null : 'Enter an email address.'
    case 'jira.project_key':
      return JIRA_PROJECT_KEY_RE.test(text.toUpperCase())
        ? null
        : 'Use 2–32 letters, digits or underscores, starting with a letter (e.g. ENG).'
    case 'linear.team_id':
      return LINEAR_ID_RE.test(text)
        ? null
        : 'Use up to 64 letters, digits, dashes or underscores (the team id or key).'
    default:
      return null
  }
}

export function trackerDraftInvalid(draft: TrackerDraft): boolean {
  return (Object.entries(draft) as Array<[TrackerDefaultKey, string | null]>).some(
    ([key, value]) => trackerFieldError(key, value) !== null,
  )
}

/** The PATCH body: only what changed; `null` clears, a blank secret is left out. */
export function buildTrackerUpdate(draft: TrackerDraft): OrgTrackerDefaultsUpdate {
  const update: Record<TrackerKind, Record<string, string | null>> = { jira: {}, linear: {} }
  for (const [key, value] of Object.entries(draft) as Array<[TrackerDefaultKey, string | null]>) {
    const [tracker, field] = split(key)
    if (value === null) {
      update[tracker][field] = null
    } else if (TRACKER_SECRET_KEYS.has(key)) {
      if (value.trim() !== '') update[tracker][field] = value
    } else {
      update[tracker][field] = value.trim()
    }
  }
  const body: OrgTrackerDefaultsUpdate = {}
  if (Object.keys(update.jira).length > 0) body.jira = update.jira
  if (Object.keys(update.linear).length > 0) body.linear = update.linear
  return body
}

export function trackerUpdateEmpty(update: OrgTrackerDefaultsUpdate): boolean {
  return !update.jira && !update.linear
}

/**
 * Jira's site, account and token are one unit: a project that sets any of
 * them in its own config inherits none of the three, so the organization's
 * token never goes to a site the project named (critique #13).
 */
const JIRA_ENDPOINT_KEYS: readonly TrackerDefaultKey[] = [
  'jira.base_url',
  'jira.auth_email',
  'jira.api_token',
]

/**
 * The Jira group note: the organization will hold some of
 * {@link JIRA_ENDPOINT_KEYS} but not all three after this save, such as a
 * site or account without a token (projects inheriting the site then have no
 * credential for it), or a token with no site.
 */
export function jiraGroupWarning(defaults: OrgTrackerDefaults, draft: TrackerDraft): string | null {
  const will = (key: TrackerDefaultKey): boolean => {
    if (key in draft) {
      const value = draft[key]
      if (value === null || value === undefined) return false
      if (TRACKER_SECRET_KEYS.has(key) && value.trim() === '') return secretConfigured(defaults, key)
      return value.trim() !== ''
    }
    return trackerSource(defaults, key) === 'org'
  }
  const held = JIRA_ENDPOINT_KEYS.filter(will).length
  if (held === 0 || held === JIRA_ENDPOINT_KEYS.length) return null
  return 'The Jira site, account email and API token go together: projects that inherit them need all three, and a project that sets any of them in its own config uses none of the organization’s.'
}
