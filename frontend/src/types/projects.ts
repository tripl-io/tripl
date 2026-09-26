import type { ProjectLatestScanJob, ProjectLatestSignal } from './scans'

export interface EventTypeOwner {
  id: string
  event_type_id: string
  user_id: string
  user_email: string
  user_name: string
  granted_by: string | null
  created_at: string
}

export interface ProjectSummary {
  event_type_count: number
  event_count: number
  active_event_count: number
  implemented_event_count: number
  review_pending_event_count: number
  archived_event_count: number
  variable_count: number
  scan_count: number
  // Metric definitions in the project, any status: the "Define a key metric"
  // onboarding step's done-state (JR-2). Always sent; optional so summaries
  // built before it still type, and a missing count leaves that step out.
  metric_count?: number
  alert_destination_count: number
  // Enabled alert rules across this project's destinations. A destination on
  // its own routes nothing, so "alerting is set up" needs both counters.
  alert_rule_count: number
  monitoring_signal_count: number
  firing_monitor_count: number
  // Incidents still awaiting triage (inbox status 'open'). `firing_monitor_count`
  // counts monitors that are unhappy right now; this counts work a human still
  // owes an answer on, which is the number the alerting tab is judged by.
  open_incident_count: number
  // Number of scan configs whose latest run failed. Counts hidden per-config
  // failures the single newest `latest_scan_job` misses.
  failing_scan_config_count: number
  // Enabled alert destinations whose latest delivery failed: the per-channel
  // twin of `failing_scan_config_count`, for the Overview status line (MO-15).
  // Always sent (default 0); optional so summaries built before it still type.
  failing_alert_destination_count?: number
  latest_scan_job: ProjectLatestScanJob | null
  latest_signal: ProjectLatestSignal | null
}

/**
 * Provisioning lifecycle of a generated (demo) project. Ordinary projects are
 * always `'ready'`; a demo is `'seeding'`/`'pending'` only transiently and a
 * failed demo is rolled back / hidden, so the UI treats a listed demo as
 * effectively `'ready'` unless the response says otherwise.
 */
export type ProjectGenerationStatus = 'ready' | 'failed' | 'seeding' | 'pending'

export interface Project {
  id: string
  name: string
  slug: string
  description: string
  app_version_keep_releases: number
  // IANA zone every wall-clock schedule in this project is read in — today,
  // alert delivery cadences. Optional on the wire so fixtures written before
  // the column keep type-checking; the server always sends it.
  timezone?: string
  created_at: string
  updated_at: string
  summary: ProjectSummary
  // Demo identity + lifecycle (tripl-2su6). Optional on the wire for
  // backward-compatible fixtures; real responses always carry is_demo /
  // generation_status (which default to false / 'ready').
  is_demo?: boolean
  generation_status?: ProjectGenerationStatus
  generation_stage?: string | null
  generation_error?: string | null
  demo_recipe_version?: string | null
  // Seed time, floored to the hour (the runtime tick anchors its bucket grid to
  // it), so it is NOT a freshness stamp — a demo seeded at 10:59 carries 10:00.
  demo_seeded_at?: string | null
  // When the runtime tick last advanced this demo's data; null until the first
  // tick. The only honest freshness signal we have (tripl-2su6.17).
  demo_last_tick_at?: string | null
  created_by_user_id?: string | null
  // Whether the signed-in caller may write inside this project: the backend's
  // mutation gate answered per request (instance owner, or a member with an
  // editing role whose instance role is not viewer). Optional so fixtures that
  // predate it keep type-checking; lib/permissions falls back to `my_role`, then
  // the role/demo rule, when it is absent.
  can_mutate?: boolean
  // The caller's access to this project: 'owner' for an instance owner (who
  // sees every project), otherwise their project membership role, capped by
  // their instance role. Optional so fixtures that predate project membership
  // keep type-checking; the server always sends it.
  my_role?: ProjectAccessRole
}

/** A project membership role. The instance owner is never a member row. */
export type ProjectMemberRole = 'editor' | 'viewer'

/** What `ProjectResponse.my_role` can say about the caller. */
export type ProjectAccessRole = 'owner' | ProjectMemberRole

/** One row of `GET /projects/{slug}/members`. Hand-written until the API
 *  types are regenerated. */
export interface ProjectMember {
  user_id: string
  name: string | null
  email: string
  role: ProjectMemberRole
  added_at: string
}

export type ActivityItemType = 'anomaly' | 'scan' | 'alert' | 'event'
export type ActivityItemSeverity = 'high' | 'medium' | 'low'

export interface ActivityItem {
  id: string
  project_id: string
  project_slug: string
  project_name: string
  type: ActivityItemType
  severity: ActivityItemSeverity
  title: string
  detail: string
  occurred_at: string
  target_path: string | null
}
