import { api } from './client'
import type { AnomalyScopeOverrideList, ProjectAnomalySettings } from '../types'

export const anomalySettingsApi = {
  get: (slug: string) =>
    api.get<ProjectAnomalySettings>(`/projects/${slug}/anomaly-settings`),

  update: (
    slug: string,
    data: Partial<{
      anomaly_detection_enabled: boolean
      detect_project_total: boolean
      detect_event_types: boolean
      detect_events: boolean
      detect_metrics: boolean
      baseline_window_buckets: number
      min_history_buckets: number
      sigma_threshold: number
      min_expected_count: number
      recent_signal_window_hours: number
      anomaly_ingestion_settling_minutes: number
      holiday_country: string | null
    }>,
  ) => api.patch<ProjectAnomalySettings>(`/projects/${slug}/anomaly-settings`, data),

  /** Country codes a holiday calendar can be set to. */
  holidayCountries: (slug: string) =>
    api.get<string[]>(`/projects/${slug}/anomaly-settings/holiday-countries`),

  listScopeOverrides: (slug: string) =>
    api.get<AnomalyScopeOverrideList>(`/projects/${slug}/anomaly-settings/scope-overrides`),

  /** Undo one false-positive ratchet — the scope returns to the project setting. */
  deleteScopeOverride: (slug: string, overrideId: string) =>
    api.del<void>(`/projects/${slug}/anomaly-settings/scope-overrides/${overrideId}`),
}
