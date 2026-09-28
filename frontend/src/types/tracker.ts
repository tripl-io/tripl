/** The trackers an implementation ticket can be opened in (#258 adds Linear). */
export type TrackerType = 'jira' | 'linear'

/**
 * Per-project implementation tracker (Jira or Linear) connection config.
 *
 * The raw API token (Jira) or API key (Linear) is NEVER returned by the API —
 * `api_token_set` is the only signal that one is stored; both trackers keep
 * their secret in that one encrypted, owner-gated slot. `id`/timestamps are null while the project rides
 * the defaults (the row materializes on the first PATCH).
 */
export interface ProjectTrackerConfig {
  id: string | null
  project_id: string
  enabled: boolean
  tracker_type: string
  base_url: string
  project_key: string
  auth_email: string
  issue_type: string
  /** Linear team the issues are created in. Absent on a server that predates
   *  Linear trackers (#258); unused by Jira. */
  team_id?: string | null
  api_token_set: boolean
  /** Fields the project leaves empty and takes from its organization's tracker
   *  defaults (F20 PR12): `base_url`, `auth_email`, `api_token`, `project_key`
   *  for Jira; `api_token`, `team_id` for Linear. */
  inherited_fields?: string[]
  created_at: string | null
  updated_at: string | null
}

/**
 * Partial update for the tracker config (owner-only on the backend). Every
 * field is optional. `api_token` is the RAW token — only send it when the user
 * actually typed one; the backend rejects an empty string and cannot clear a
 * stored token via PATCH, so omit it to preserve the existing token.
 */
export interface ProjectTrackerConfigUpdate {
  enabled?: boolean
  tracker_type?: TrackerType
  base_url?: string
  project_key?: string
  auth_email?: string
  api_token?: string
  issue_type?: string
  /** Linear only. */
  team_id?: string
}

/**
 * One tracker ticket opened when a plan branch merged, as returned by
 * `GET /projects/{slug}/branches/{branch_id}/implementation-tickets`.
 *
 * Written only by the backend (merge worker + poll sync), so every field here
 * is read-only. `status` is `open` until the tracker reports the issue done, at
 * which point sync flips it to `closed` and stamps `closed_at`; it is a plain
 * string because the backend column is one too. `external_url` is `''` when the
 * tracker answered without an issue key — render the key as text, not a link.
 */
export interface ImplementationTicket {
  id: string
  project_id: string
  branch_id: string
  tracker_type: string
  external_id: string | null
  external_key: string | null
  external_url: string
  status: string
  summary: string
  /** Main-branch event ids the ticket covers; they advance to `implemented`
   * when the ticket closes. */
  event_ids: string[]
  created_at: string
  updated_at: string
  closed_at: string | null
}
