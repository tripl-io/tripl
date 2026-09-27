import { api } from './client'
import type { ProjectTemplateSummary } from '../types/projectTemplates'

/**
 * Project templates (F21, #274). Workspace-wide, not project-scoped: the list
 * feeds the New project dialog before any project exists. The chosen id goes
 * into `projectsApi.create({ template_id })`.
 */
export const projectTemplatesApi = {
  list: (signal?: AbortSignal) =>
    api.get<ProjectTemplateSummary[]>('/project-templates', signal),
}
