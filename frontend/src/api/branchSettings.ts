import { api } from './client'
import type { ProjectBranchSettings } from '../types'
import type { components } from '../types/api.gen'

export const branchSettingsApi = {
  get: (slug: string) =>
    api.get<ProjectBranchSettings>(`/projects/${slug}/branch-settings`),

  update: (
    slug: string,
    data: components['schemas']['ProjectBranchSettingsUpdate'],
  ) => api.patch<ProjectBranchSettings>(`/projects/${slug}/branch-settings`, data),
}
