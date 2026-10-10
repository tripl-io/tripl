/**
 * How the audit log reads its rows: the chip tone and past-tense sentence for an
 * action code, the target a row links to, the labels of its payload, the day
 * headers and time of day the list groups under, and the zone those times are
 * in. Kept apart from AuditTab.tsx so the page component stays one screen of
 * layout.
 */
import type { ChipTone } from '@/components/primitives/chip'
import { formatDate } from '@/lib/datetime'
import type { AuditEntry } from '@/types'
import { currentOrgSlug, projectPath } from '@/lib/navigation'
import { stateKeyLabel } from './branches/branchMeta'

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
 * The verb is followed by the target's noun, so a phrase that needs an
 * object ends on its preposition ("Added a note to" + "incident"). An
 * unknown verb is humanised; the raw code stays in the chip's title. */
const VERB_PAST: Record<string, string> = {
  create: 'Created',
  update: 'Updated',
  delete: 'Deleted',
  // A bulk code has to read apart from its single-row sibling, or the Action
  // filter offers two identical entries. Every bulk code recorded today has a
  // whole sentence below ("Deleted events in bulk"); these cover a new one.
  bulk_create: 'Bulk-created',
  bulk_update: 'Bulk-updated',
  bulk_delete: 'Bulk-deleted',
  merge: 'Merged',
  approve: 'Approved',
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
  unacknowledge: 'Unacknowledged',
  resolve: 'Resolved',
  apply: 'Applied',
  reset: 'Reset',
  rename: 'Renamed',
  run: 'Ran',
  collect: 'Collected',
  preview: 'Previewed',
  test: 'Sent a test to',
  retry: 'Retried',
  upload: 'Uploaded',
  note: 'Added a note to',
  notify_owners: 'Notified the owners of',
  verdict: 'Gave a verdict on',
  clear_verdict: 'Cleared the verdict on',
  comment_create: 'Commented on',
  comment_delete: 'Deleted a comment on',
  rotate_secret: 'Rotated the secret of',
  add_reviewer: 'Added a reviewer to',
  remove_reviewer: 'Removed a reviewer from',
  add_owner: 'Added an owner to',
  remove_owner: 'Removed an owner from',
  role_update: 'Changed the role of',
  // Project membership: the subject is the project, the member
  // is the row's target.
  member_add: 'Added a member to',
  member_update: 'Changed a member’s role on',
  member_role_update: 'Changed a member’s role in',
  member_remove: 'Removed a member from',
  transfer_ownership: 'Transferred the ownership of',
  // Docs catalog (F22).
  move: 'Moved',
  restore: 'Restored',
  import: 'Imported',
}

/**
 * Whole-action sentences where verb + noun would read wrong: a folder delete
 * and an import act on many notes, not one (F22); a bulk edit acts on many
 * rows; a compound verb (`values_clear`, `update_from_main`) has no past
 * tense of its own. Each one still ends on a noun, never a preposition: it is
 * also the Action filter's option label, where no target follows.
 */
const ACTION_SENTENCE: Record<string, string> = {
  // Events.
  'event.bulk_create': 'Created events in bulk',
  'event.bulk_update': 'Updated events in bulk',
  'event.bulk_delete': 'Deleted events in bulk',
  'event.duplicate_dismiss': 'Dismissed a duplicate suggestion for event',
  'event_comment.create': 'Commented on an event',
  'event_comment.delete': 'Deleted a comment on an event',
  // Resolve, snooze or reopen: the payload names which.
  'event_comment.action': 'Changed the status of a comment',
  'event_photo.figma_attach': 'Attached a Figma frame to an event',
  'event_photo.reorder': 'Reordered the photos of event',
  // Schema.
  'schema_drift.false_positive': 'Marked a schema drift as a false positive',
  // Properties: `variable` is the code's name for them.
  'variable.bulk_update': 'Updated properties in bulk',
  'variable.bulk_delete': 'Deleted properties in bulk',
  'variable.values_clear': 'Cleared the values of property',
  'variable.override_set': 'Set an event override on property',
  'variable.override_delete': 'Removed an event override from property',
  'variable.override_bulk_set': 'Set event overrides in bulk on property',
  'variable.override_bulk_delete': 'Removed event overrides in bulk from property',
  'variable.drift_action': 'Reviewed a value drift on property',
  'variable.property_drift_action': 'Reviewed a property drift on property',
  // Versioning.
  'plan_revision.create': 'Saved a plan snapshot',
  'plan_branch.submit': 'Submitted branch for review',
  'plan_branch.update_from_main': 'Updated branch from main',
  'plan_branch.transfer_out': 'Moved or copied changes out of branch',
  'plan_branch.transfer_in': 'Moved or copied changes into branch',
  'plan_branch.resolution_save': 'Resolved a conflict on branch',
  'plan_branch.resolution_batch_save': 'Resolved conflicts on branch',
  'plan_branch.resolution_delete': 'Cleared a conflict resolution on branch',
  // Scans, metrics and their previews.
  'scan_config.metrics_replay': 'Replayed metrics for scan',
  'scan_config.event_groups.apply': 'Applied event groups from scan',
  'metric_definition.bulk_update': 'Updated metrics in bulk',
  'metric.preview': 'Previewed a SQL metric',
  'metric.fact_preview': 'Previewed a metric filter on fact table',
  'metric.series_preview': 'Previewed a metric series',
  // Alerting and signals.
  'alert_inbox.false_positive': 'Marked an incident as a false positive',
  'signal.mark_expected': 'Marked a signal as expected',
  'signal.unmark_expected': 'Unmarked a signal as expected',
  // Docs.
  'doc.folder_delete': 'Deleted a folder of notes',
  'doc.import': 'Imported notes',
  'doc.restore': 'Restored an earlier revision of note',
  'doc.share_update': 'Changed the sharing of note',
  // An org owner or admin opening a note hidden from them (F24).
  'doc.break_glass_read': 'Used break-glass access to read note',
  'doc.translate': 'Asked for an AI translation of note',
  'doc.translation_edit': 'Edited a translation of note',
  'doc.translation_restore': 'Restored an earlier translation of note',
  'doc.translation_delete': 'Deleted a translation of note',
  // Project.
  'project.reset_anomalies': 'Reset the anomalies of project',
  'project.reset_drifts': 'Reset the drifts of project',
  'project.retire_unused_variables': 'Retired the unused properties of project',
  'project.docs_languages': 'Changed the default docs languages of project',
  // Workspace.
  'user.invite_revoke': 'Revoked an invitation',
  'user.invite_accept': 'Accepted an invitation',
  'user.password_reset_link': 'Created a password reset link',
  'settings.update': 'Changed workspace settings',
  // Written by the removed AI settings endpoint; older entries carry it.
  'settings.ai_update': 'Changed AI settings',
  // Organization (Enterprise).
  'org.delete_request': 'Requested deletion of organization',
  'org.delete_cancel': 'Cancelled deletion of organization',
  'org.delete_complete': 'Completed deletion of organization',
  'org.audit_export': 'Exported the audit log',
  // Single sign-on and SCIM provisioning (Enterprise). A SCIM row carries no
  // user: the identity provider made the change through a token.
  'org.sso.update': 'Changed single sign-on settings',
  'org.sso.domain_add': 'Added a single sign-on domain',
  'org.sso.domain_verify': 'Verified a single sign-on domain',
  'org.sso.domain_remove': 'Removed a single sign-on domain',
  'user.sso_login': 'Signed in with single sign-on',
  'user.sso_provision': 'Joined through single sign-on',
  'user.sso_link': 'Linked an account to single sign-on',
  'user.google_sign_in': 'Signed in with Google',
  'user.oidc_sign_in': 'Signed in with OpenID Connect',
  'org.scim.token_create': 'Created a SCIM token',
  'org.scim.token_revoke': 'Revoked a SCIM token',
  'org.scim.config_update': 'Changed SCIM settings',
  'org.scim.user_provision': 'Provisioned a user through SCIM',
  'org.scim.user_link': 'Linked a user through SCIM',
  'org.scim.user_update': 'Updated a user through SCIM',
  'org.scim.user_deactivate': 'Deactivated a user through SCIM',
  'org.scim.user_reactivate': 'Reactivated a user through SCIM',
  'org.scim.group_create': 'Created a group through SCIM',
  'org.scim.group_update': 'Updated a group through SCIM',
  'org.scim.group_delete': 'Deleted a group through SCIM',
  // The platform console (F20): an operator acting on the organization, which
  // its owners read in their own audit log.
  'org.suspend': 'Suspended the organization',
  'org.unsuspend': 'Reinstated the organization',
  'platform.step_in': 'Started a read-only step-in',
  'platform.step_in_end': 'Ended a read-only step-in',
  'platform.admin_grant': 'Granted platform admin to user',
  'platform.admin_revoke': 'Revoked platform admin from user',
  'platform.license_set': 'Installed the Enterprise license',
  'platform.license_clear': 'Removed the Enterprise license',
  'org.audit_retention.update': 'Changed how long the audit log is kept',
  // Escalation (Enterprise): the organization's policies and alert routes.
  'org.escalation_policy.create': 'Created the escalation policy',
  'org.escalation_policy.update': 'Changed the escalation policy',
  'org.escalation_policy.delete': 'Deleted the escalation policy',
  'org.alert_route.create': 'Created the alert route',
  'org.alert_route.update': 'Changed the alert route',
  'org.alert_route.delete': 'Deleted the alert route',
  // Plan governance (Enterprise): the organization's plan policies.
  'org.governance_policy.create': 'Created the plan policy',
  'org.governance_policy.update': 'Changed the plan policy',
  'org.governance_policy.delete': 'Deleted the plan policy',
  // Access control (Enterprise): custom roles, group grants and team sync.
  'org.project_role.create': 'Created the custom role',
  'org.project_role.update': 'Changed the custom role',
  'org.project_role.delete': 'Deleted the custom role',
  'org.group_grant.create': 'Gave a group a role in a project',
  'org.group_grant.update': "Changed a group's role in a project",
  'org.group_grant.delete': "Removed a group's role in a project",
  'org.team_sync.update': 'Changed team sync',
  'org.team_sync.apply': 'Synced groups from the identity provider for user',
}

/** The noun for an action's subject and for a row's `target_type`, in the
 * words the rest of the app uses: a `variable` is a property everywhere a
 * person reads it, an `alert_inbox` entry an incident. */
export const TARGET_NOUN: Record<string, string> = {
  plan_branch: 'branch',
  plan_branch_settings: 'branch settings',
  event_type: 'event type',
  event_photo: 'photo',
  field_definition: 'field',
  meta_field: 'meta field',
  variable: 'property',
  schema_drift: 'schema drift',
  metric_definition: 'metric',
  fact_table: 'fact table',
  shadow_event: 'shadow event',
  alert_rule: 'alert rule',
  alert_destination: 'alert destination',
  alert_delivery: 'alert delivery',
  alert_inbox: 'incident',
  anomaly_settings: 'anomaly settings',
  anomaly_scope_override: 'anomaly scope override',
  chart_annotation: 'annotation',
  // "Planned event" is an event of the tracking plan; the app calls these
  // anomaly windows "expected windows".
  planned_event: 'expected window',
  project_tracker_config: 'implementation tracker',
  scan_config: 'scan',
  scan_job: 'scan run',
  data_source: 'data source',
  api_key: 'API key',
  doc: 'note',
  org: 'organization',
  'org.group': 'group',
  'org.audit_webhook': 'audit webhook',
}

/** A code as words: the fallback for a verb or noun nothing above names. Dots
 * go too, so a nested code (`scan_config.event_groups`) never leaks one. */
export function humanize(code: string): string {
  return code.replace(/[_.]/g, ' ')
}

/** The targets whose payload is a plan entity's stored state. */
const PLAN_ENTITY_TARGETS: ReadonlySet<string> = new Set([
  'event',
  'event_type',
  'field_definition',
  'variable',
  'meta_field',
  'relation',
])

/**
 * The label of one payload key of an entry about `targetType`. A plan entity's
 * keys read as the plan's own pages name them (`stateKeyLabel`), so an
 * event's `reviewed` is "Verified". Any other entity's keys are its own words
 * in sentence case: a metric's `reviewed` flag is "Reviewed".
 */
export function payloadKeyLabel(key: string, targetType: string): string {
  if (PLAN_ENTITY_TARGETS.has(targetType)) return stateKeyLabel(key)
  return humanize(key).replace(/^./, (c) => c.toUpperCase())
}

/** `scan_config.event_groups.apply` → the subject before the LAST dot, the
 * verb after it. */
function splitAction(action: string): { type: string; verb: string } {
  const dot = action.lastIndexOf('.')
  return dot >= 0
    ? { type: action.slice(0, dot), verb: action.slice(dot + 1) }
    : { type: action, verb: '' }
}

/** "Updated event", "Approved branch" — the verb and the kind of thing. */
export function actionSentence(action: string): string {
  const whole = ACTION_SENTENCE[action]
  if (whole) return whole
  const { type, verb } = splitAction(action)
  const past = VERB_PAST[verb] ?? (verb ? humanize(verb).replace(/^./, (c) => c.toUpperCase()) : '')
  const noun = TARGET_NOUN[type] ?? humanize(type)
  return past ? `${past} ${noun}` : noun
}

/**
 * Whether `actionSentence` reads this code from a written sentence or a known
 * verb, rather than humanising it. Every code the backend's catalog serves is
 * held to this by auditSentences.catalog.test.ts, so a new action fails a test instead
 * of shipping as "Values clear variable".
 */
export function hasWrittenSentence(action: string): boolean {
  return Object.hasOwn(ACTION_SENTENCE, action) || Object.hasOwn(VERB_PAST, splitAction(action).verb)
}

/**
 * The Action filter's option labels: the same sentence the row chip shows, not
 * the code. Every code in the catalog reads differently today (the test pins
 * it), so the bracketed code below is a last resort: a menu with two identical
 * entries cannot be chosen from, so a future pair that reads alike carries its
 * code until it gets a sentence of its own.
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
