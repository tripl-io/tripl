import { api, withBranch } from './client'
import type { Project } from '../types'
import type { components } from '../types/api.gen'
import type { ProjectCreateInput, ProjectCreateResult } from '../types/projectTemplates'

/** Optional half-open window (`after <= t < before`) for a danger-zone reset. */
export interface DetectionResetPeriod {
  before?: string | null
  after?: string | null
  /** Count what the reset would delete, and delete nothing (ST-39). */
  dry_run?: boolean
}

/** Per-table rows removed by a project-wide anomaly reset. */
export interface AnomalyResetCounts {
  metric_anomalies: number
  metric_breakdown_anomalies: number
  metric_baselines: number
}

/** Outcome of asking an in-flight demo provision to abandon itself. Derived
 *  from the generated schema so the gen:api drift check covers it (DEMO-29). */
export type DemoCancelResult = components['schemas']['DemoCancelResponse']

/** What a variable-retirement pass did, and why it spared what it spared.
 *  The `kept_*` fields mirror the backend's `KeptReason`; a dry run fills
 *  everything except `retired`. */
export interface VariableRetirementCounts {
  scanned: number
  retirable: number
  retired: number
  kept_referenced: number
  kept_observed: number
  kept_documented: number
  kept_user_edited: number
  kept_excluded: number
}

/** Per-table rows removed by a project-wide drift reset. */
export interface DriftResetCounts {
  schema_drifts: number
  distribution_drifts: number
}

export const projectsApi = {
  list: (signal?: AbortSignal) => api.get<Project[]>('/projects', signal),
  // `branchId` scopes the summary's plan counters (event types, events,
  // variables) to that working branch, so the Overview's KPIs agree with the
  // branch's own lists (SH-11). Omitted, they are main's.
  get: (slug: string, signal?: AbortSignal, branchId?: string | null) =>
    api.get<Project>(withBranch(`/projects/${slug}`, branchId), signal),
  // With a `template_id` the server also opens the template's plan as a draft
  // branch and returns its id as `template_branch_id` (F21, #274); main stays
  // empty. Without one the field is null.
  create: (data: ProjectCreateInput) => api.post<ProjectCreateResult>('/projects', data),
  // Demo lifecycle. Create BLOCKS while seeding (for about
  // DEMO_PROVISION_EXPECTED_MS, demo/provisioningPhases.ts) and returns a
  // fully-ready project (201) or 500 on failure. Reset/delete are scoped to the
  // demo endpoints and permitted for the demo's creator or a workspace owner —
  // distinct from the owner-only generic DELETE /projects/{slug} (`del`).
  // Seeding a demo is the one long, blocking POST in the app — the caller passes
  // a signal so it can be timed out or cancelled instead of hanging forever.
  createDemo: (signal?: AbortSignal) => api.post<Project>('/projects/demo', {}, signal),
  // Aborting the create only stops the BROWSER reading the response — the server
  // finishes seeding regardless. This asks it to abandon the provision instead;
  // `cancelled` is false when it was already too late.
  cancelDemo: () => api.post<DemoCancelResult>('/projects/demo/cancel', {}),
  // Reset re-seeds just as long as a create, so it takes a signal for the same
  // timeout (DEMO-4).
  resetDemo: (slug: string, signal?: AbortSignal) =>
    api.post<Project>(`/projects/demo/${slug}/reset`, {}, signal),
  deleteDemo: (slug: string) => api.del(`/projects/demo/${slug}`),
  update: (slug: string, data: {
    name?: string
    slug?: string
    description?: string
    app_version_keep_releases?: number
    timezone?: string
  }) =>
    api.patch<Project>(`/projects/${slug}`, data),
  del: (slug: string) => api.del(`/projects/${slug}`),
  // Owner-only danger-zone resets: clear a whole category of detections across
  // the project within the chosen period. Destructive and irreversible.
  resetAnomalies: (slug: string, period: DetectionResetPeriod) =>
    api.post<AnomalyResetCounts>(`/projects/${slug}/danger/reset-anomalies`, period),
  resetDrifts: (slug: string, period: DetectionResetPeriod) =>
    api.post<DriftResetCounts>(`/projects/${slug}/danger/reset-drifts`, period),
  /** Owner-only: drop the variables a scan minted that nothing refers to.
   *  `dry_run` is passed explicitly so the preview and the commit are visibly
   *  two different calls rather than one call with a hidden default. */
  retireUnusedVariables: (slug: string, data: { mode?: 'delete' | 'exclude'; dry_run: boolean }) =>
    api.post<VariableRetirementCounts>(`/projects/${slug}/danger/retire-unused-variables`, data),
}
