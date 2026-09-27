import { api, withBranch } from './client'

/**
 * `GET /projects/{slug}/plan/export?format=jsonschema` (GH #262, F09): one
 * JSON Schema (draft 2020-12) per live event, keyed `<event_type>/<identity>`.
 * The schemas themselves are opaque to the UI — it only downloads the bundle —
 * so they stay `Record<string, unknown>` here.
 */
export interface PlanSchemaBundle {
  format: 'jsonschema'
  /** The plan revision the export reflects (a branch's base revision), or null. */
  revision: string | null
  /** The branch's name ("main" for main) and id. */
  branch: string
  branch_id: string
  /** sha256 over the exported content. */
  plan_hash: string
  schemas: Record<string, Record<string, unknown>>
}

export const planExportApi = {
  jsonSchema: (slug: string, branchId?: string | null) =>
    api.get<PlanSchemaBundle>(
      withBranch(`/projects/${slug}/plan/export?format=jsonschema`, branchId),
    ),
}
