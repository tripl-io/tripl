/**
 * How the audit log reads its rows: the chip tone and past-tense sentence for an
 * action code, the target a row links to, and the day headers and time of day
 * the list groups under. Kept apart from AuditTab.tsx so the page component
 * stays one screen of layout.
 */
import type { ChipTone } from '@/components/primitives/chip'
import { formatDate } from '@/lib/datetime'
import { APP_LOCALE } from '@/lib/format'
import type { AuditEntry } from '@/types'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

/**
 * Tone by what the verb DOES, matched on its suffix rather than as an exact word.
 *
 * Only `create`/`update`/`delete` used to be coloured, so `bulk_delete`,
 * `remove_owner`, `merge` and `close` all rendered neutral: a destructive bulk
 * action looked exactly like a snapshot. Suffix rules mean a future
 * `bulk_<verb>` lands in the right tone without this list learning it. First
 * match wins.
 */
const ACTION_TONE_RULES: { pattern: RegExp; tone: ChipTone }[] = [
  {
    pattern: /(delete|remove|remove_owner|remove_reviewer|revoke|cancel|dismiss|close|revert|reset\w*|retire_unused_variables)$/,
    tone: 'danger',
  },
  {
    pattern: /(create|add_owner|add_reviewer|member_add|invite|merge|approve|accept|override_set)$/,
    tone: 'success',
  },
  {
    pattern: /(update|apply|submit|request_changes|reopen|mute|unmute|snooze|false_positive|acknowledge|resolve|drift_action|role_update)$/,
    tone: 'warning',
  },
]

/**
 * Whole-action tones for the platform console's codes (F20), whose verbs no
 * suffix rule should learn: `step_in` and `suspend` are not generic verbs.
 */
const ACTION_TONE: Record<string, ChipTone> = {
  'org.suspend': 'danger',
  'org.unsuspend': 'success',
  'platform.step_in': 'warning',
  'platform.step_in_end': 'neutral',
  'platform.admin_grant': 'success',
  'platform.admin_revoke': 'danger',
  'platform.license_set': 'success',
  'platform.license_clear': 'danger',
  'org.audit_retention.update': 'warning',
}

export function actionTone(action: string): ChipTone {
  const whole = ACTION_TONE[action]
  if (whole) return whole
  const verb = action.split('.').pop() ?? ''
  return ACTION_TONE_RULES.find((rule) => rule.pattern.test(verb))?.tone ?? 'neutral'
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

// Some audit targets (e.g. scan_job.cancel) record a raw UUID as the name.
// A full UUID is unreadable in a dense row, so show a short prefix instead.
export function displayTarget(entry: { target_name?: string | null; target_type: string }): string {
  const name = entry.target_name
  if (!name) return entry.target_type
  return UUID_RE.test(name) ? name.slice(0, 8) : name
}

/** Past-tense verbs for the action codes, so a row reads as a sentence
 * ("Approved branch") instead of a server log line (`plan_branch.approve`).
 * An unknown verb is humanised; the raw code stays in the chip's
 * title. */
const VERB_PAST: Record<string, string> = {
  create: 'Created',
  update: 'Updated',
  delete: 'Deleted',
  bulk_delete: 'Deleted',
  bulk_update: 'Updated',
  merge: 'Merged',
  approve: 'Approved',
  submit: 'Submitted for review',
  request_changes: 'Requested changes on',
  reopen: 'Reopened',
  close: 'Closed',
  revert: 'Reverted a change on',
  dismiss: 'Dismissed',
  accept: 'Accepted',
  invite: 'Invited',
  revoke: 'Revoked',
  cancel: 'Cancelled',
  mute: 'Muted',
  unmute: 'Unmuted',
  snooze: 'Snoozed',
  acknowledge: 'Acknowledged',
  resolve: 'Resolved',
  apply: 'Applied',
  add_reviewer: 'Added a reviewer to',
  remove_reviewer: 'Removed a reviewer from',
  add_owner: 'Added an owner to',
  remove_owner: 'Removed an owner from',
  role_update: 'Changed the role of',
  // Project membership: the subject is the project, the member
  // is the row's target.
  member_add: 'Added a member to',
  member_update: 'Changed a member’s role on',
  member_remove: 'Removed a member from',
  // Docs catalog (F22).
  move: 'Moved',
  restore: 'Restored',
  import: 'Imported',
}

/**
 * Whole-action sentences where verb + noun would read wrong: a folder delete
 * and an import act on many notes, not one (F22).
 */
const ACTION_SENTENCE: Record<string, string> = {
  'doc.folder_delete': 'Deleted a folder of notes',
  'doc.import': 'Imported notes',
  'doc.restore': 'Restored an earlier revision of note',
  'doc.translate': 'Asked for an AI translation of note',
  'doc.translation_edit': 'Edited a translation of note',
  'doc.translation_restore': 'Restored an earlier translation of note',
  'doc.translation_delete': 'Deleted a translation of note',
  'project.docs_languages': 'Changed the default docs languages of',
  // The platform console (F20): an operator acting on the organization, which
  // its owners read in their own audit log.
  'org.suspend': 'Suspended the organization',
  'org.unsuspend': 'Reinstated the organization',
  'platform.step_in': 'Started a read-only step-in',
  'platform.step_in_end': 'Ended a read-only step-in',
  'platform.admin_grant': 'Granted platform admin to',
  'platform.admin_revoke': 'Revoked platform admin from',
  'platform.license_set': 'Installed the Enterprise license',
  'platform.license_clear': 'Removed the Enterprise license',
  'org.audit_retention.update': 'Changed how long the audit log is kept',
}

export const TARGET_NOUN: Record<string, string> = {
  plan_branch: 'branch',
  event_type: 'event type',
  field_definition: 'field',
  meta_field: 'meta field',
  metric_definition: 'metric',
  shadow_event: 'shadow event',
  alert_rule: 'alert rule',
  alert_destination: 'alert destination',
  scan_config: 'scan',
  scan_job: 'scan run',
  data_source: 'data source',
  api_key: 'API key',
  doc: 'note',
}

export function humanize(code: string): string {
  return code.replace(/_/g, ' ')
}

/** "Updated event", "Approved branch" — the verb and the kind of thing. */
export function actionSentence(action: string): string {
  const whole = ACTION_SENTENCE[action]
  if (whole) return whole
  const dot = action.lastIndexOf('.')
  const type = dot >= 0 ? action.slice(0, dot) : action
  const verb = dot >= 0 ? action.slice(dot + 1) : ''
  const past = VERB_PAST[verb] ?? (verb ? humanize(verb).replace(/^./, (c) => c.toUpperCase()) : '')
  const noun = TARGET_NOUN[type] ?? humanize(type)
  return past ? `${past} ${noun}` : noun
}

/**
 * The Action filter's option labels: the same sentence the row chip shows, not
 * the code. Two codes can read alike (`event.delete` and
 * `event.bulk_delete` are both "Deleted event"), and a menu with two identical
 * entries cannot be chosen from, so only those carry their code in brackets.
 */
export function actionOptionLabels(actions: readonly string[]): Map<string, string> {
  const sentences = actions.map((a) => [a, actionSentence(a)] as const)
  const seen = new Map<string, number>()
  for (const [, sentence] of sentences) seen.set(sentence, (seen.get(sentence) ?? 0) + 1)
  return new Map(
    sentences.map(([a, sentence]) => [a, (seen.get(sentence) ?? 0) > 1 ? `${sentence} (${a})` : sentence]),
  )
}

/** Where a row's target lives, for the targets that have a page. None for a
 * deletion: the thing is gone. */
export function targetPath(entry: AuditEntry): string | null {
  if (!entry.project_slug || !entry.target_id || entry.action.endsWith('delete')) return null
  const base = projectPath(currentOrgSlug(), entry.project_slug)
  switch (entry.target_type) {
    case 'event':
      return `${base}/events/all/${entry.target_id}`
    case 'event_type':
      return `${base}/event-types/${entry.target_id}`
    case 'variable':
      return `${base}/variables/${entry.target_id}`
    case 'plan_branch':
      return `${base}/branches/${entry.target_id}`
    default:
      return null
  }
}

/** "Today", "Yesterday" or the date, for the day headers. */
export function dayLabel(iso: string, now = new Date()): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const key = (d: Date) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (key(date) === key(now)) return 'Today'
  if (key(date) === key(yesterday)) return 'Yesterday'
  return formatDate(iso)
}

export function timeOfDay(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString(APP_LOCALE, { hour: 'numeric', minute: '2-digit' })
}

/** Consecutive entries of one local day, in list order. */
export function groupByDay(entries: AuditEntry[]): { label: string; entries: AuditEntry[] }[] {
  const groups: { label: string; entries: AuditEntry[] }[] = []
  for (const entry of entries) {
    const label = dayLabel(entry.created_at)
    const last = groups[groups.length - 1]
    if (last && last.label === label) last.entries.push(entry)
    else groups.push({ label, entries: [entry] })
  }
  return groups
}

export function toIsoOrUndef(localDateTime: string, endOfDay = false): string | undefined {
  if (!localDateTime) return undefined
  // <input type="date"> gives YYYY-MM-DD without time; pin to start/end of day.
  const iso = `${localDateTime}T${endOfDay ? '23:59:59.999' : '00:00:00.000'}`
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString()
}
