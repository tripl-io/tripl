import { ApiError, api } from './client'
import type {
  EventGroupRule,
  PlatformPresenceResponse,
  ScanActivityResponse,
  ScanConfig,
  ScanConfigPreview,
  ScanDryRunJob,
  ScanDryRunRequest,
  ScanDryRunResponse,
  ScanJob,
  ScanPreviewJob,
} from '../types'

const PREVIEW_POLL_INTERVAL_MS = 1500
const PREVIEW_POLL_TIMEOUT_MS = 5 * 60 * 1000
/**
 * Failed polls in a row a job survives before the wait gives up. A job is a
 * warehouse query that keeps running on the worker whatever this loop does, so
 * one 502 or network blip mid-wait used to throw away a result that was about to
 * arrive, and the only way back was a new (billed) warehouse job.
 */
const POLL_MAX_CONSECUTIVE_FAILURES = 3

function abortError(): DOMException {
  return new DOMException('The wait for this job was cancelled.', 'AbortError')
}

/** Resolves after `ms`, or rejects as soon as `signal` aborts. */
function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError())
      return
    }
    const onAbort = () => {
      clearTimeout(timer)
      reject(abortError())
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

/**
 * Whether a failed poll is worth asking again: the gateway or the network, not
 * an answer. A 4xx (the job is gone, the session lost access) will say the same
 * thing next time, so it ends the wait straight away.
 */
function isTransientPollError(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true
  return error.status >= 500 || error.status === 408 || error.status === 429
}

/** Shape shared by the preview and dry-run poll loops (202 + poll). */
interface PollableJob<T> {
  id: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
  result_summary: T | null
  error_message: string | null
}

/**
 * Poll a 202-and-poll worker job until it resolves, returning its payload.
 *
 * Preview and dry-run are both warehouse round trips the request path refuses to
 * hold open, and both answer with the same envelope. One loop, so a timeout or a
 * cancelled job cannot mean two different things on two screens.
 *
 * Only a terminal job status or the deadline ends the wait; a transient poll
 * failure is retried with backoff. `signal` stops the wait (not the job) when
 * the draft that asked has moved on or the form unmounted.
 */
async function pollJob<T>(
  job: PollableJob<T>,
  fetchJob: (jobId: string) => Promise<PollableJob<T>>,
  timedOutMessage: string,
  failedMessage: string,
  signal?: AbortSignal,
): Promise<T> {
  const deadline = Date.now() + PREVIEW_POLL_TIMEOUT_MS
  let current = job
  let failures = 0
  while (current.status === 'pending' || current.status === 'running') {
    if (Date.now() > deadline) throw new Error(timedOutMessage)
    await sleep(PREVIEW_POLL_INTERVAL_MS * 2 ** failures, signal)
    try {
      current = await fetchJob(job.id)
      failures = 0
    } catch (error) {
      failures += 1
      if (!isTransientPollError(error) || failures >= POLL_MAX_CONSECUTIVE_FAILURES) throw error
    }
  }
  if (current.status !== 'completed' || !current.result_summary) {
    throw new Error(current.error_message || failedMessage)
  }
  return current.result_summary
}

export const scansApi = {
  list: (slug: string) =>
    api.get<ScanConfig[]>(`/projects/${slug}/scans`),

  get: (slug: string, scanId: string) =>
    api.get<ScanConfig>(`/projects/${slug}/scans/${scanId}`),

  getPlatformPresence: (slug: string, scanId: string) =>
    api.get<PlatformPresenceResponse>(`/projects/${slug}/scans/${scanId}/platform-presence`),

  create: (slug: string, data: {
    data_source_id: string
    name: string
    base_query: string
    event_type_id?: string | null
    event_type_column?: string | null
    time_column?: string | null
    event_name_format?: string | null
    json_value_paths?: string[]
    event_group_rules?: EventGroupRule[]
    metric_breakdown_columns?: string[]
    metric_breakdown_values_limit?: number | null
    distribution_drift_fields?: string[]
    cardinality_threshold?: number
    interval?: string | null
    replay_chunk_interval?: string | null
    scan_lookback_hours?: number | null
    scan_row_limit?: number | null
    metrics_row_limit?: number | null
    app_version_column?: string | null
    app_version_prerelease_pattern?: string | null
    app_version_active_share_min?: number | null
    platform_column?: string | null
  }) => api.post<ScanConfig>(`/projects/${slug}/scans`, data),

  // Preview runs against the warehouse and can be slow, so the backend handles
  // it as a worker job: POST enqueues, then we poll until it completes.
  startPreview: (slug: string, data: {
    data_source_id: string
    base_query: string
    limit?: number
    json_value_paths?: string[]
    time_column?: string | null
    scan_lookback_hours?: number | null
    include_json_paths?: boolean
    event_name_column?: string | null
    properties_column?: string | null
    json_string_columns?: string[]
  }) => api.post<ScanPreviewJob>(`/projects/${slug}/scans/preview`, data),

  getPreviewJob: (slug: string, jobId: string) =>
    api.get<ScanPreviewJob>(`/projects/${slug}/scans/preview-jobs/${jobId}`),

  // Enqueue a preview job and poll until it resolves, returning the preview
  // payload. Rejects if the job fails or polling exceeds the timeout.
  // With include_json_paths the job discovers nested JSON keys instead, and the
  // resolved payload carries only json_columns (the "Discover JSON keys" flow).
  preview: async (slug: string, data: {
    data_source_id: string
    base_query: string
    limit?: number
    json_value_paths?: string[]
    time_column?: string | null
    scan_lookback_hours?: number | null
    include_json_paths?: boolean
    event_name_column?: string | null
    properties_column?: string | null
    json_string_columns?: string[]
  }, signal?: AbortSignal): Promise<ScanConfigPreview> => {
    const job = await scansApi.startPreview(slug, data)
    return pollJob(
      job,
      jobId => scansApi.getPreviewJob(slug, jobId),
      'Preview timed out',
      'Preview failed',
      signal,
    )
  },

  // The dry run answers "what would this config create?" by pushing sampled
  // warehouse rows through the same planner a real run uses. It groups the whole
  // window rather than reading ten rows, so it is strictly slower than the
  // preview and gets the same 202-and-poll treatment.
  startDryRun: (slug: string, data: ScanDryRunRequest) =>
    api.post<ScanDryRunJob>(`/projects/${slug}/scans/dry-run`, data),

  getDryRunJob: (slug: string, jobId: string) =>
    api.get<ScanDryRunJob>(`/projects/${slug}/scans/dry-run-jobs/${jobId}`),

  dryRun: async (
    slug: string,
    data: ScanDryRunRequest,
    signal?: AbortSignal,
  ): Promise<ScanDryRunResponse> => {
    const job = await scansApi.startDryRun(slug, data)
    // Both strings reach the user, under ScanPreviewPanel's "Could not work out
    // what this scan would create". "Dry run" is the pipeline's name for this,
    // not the product's: nothing on that panel ever says it — the button says
    // Check, the wait says "Working out what this scan would create…", the
    // answer says "Would create N events". These two were the only strings that
    // named the mechanism at the user, and the failure line does not repeat the
    // heading above it.
    return pollJob(
      job,
      jobId => scansApi.getDryRunJob(slug, jobId),
      'Timed out working out what this scan would create.',
      'The check stopped without saying why.',
      signal,
    )
  },

  update: (slug: string, scanId: string, data: {
    name?: string
    base_query?: string
    event_type_id?: string | null
    event_type_column?: string | null
    time_column?: string | null
    event_name_format?: string | null
    json_value_paths?: string[]
    event_group_rules?: EventGroupRule[]
    metric_breakdown_columns?: string[]
    metric_breakdown_values_limit?: number | null
    distribution_drift_fields?: string[]
    cardinality_threshold?: number
    interval?: string | null
    replay_chunk_interval?: string | null
    scan_lookback_hours?: number | null
    scan_row_limit?: number | null
    metrics_row_limit?: number | null
    app_version_column?: string | null
    app_version_prerelease_pattern?: string | null
    app_version_active_share_min?: number | null
    platform_column?: string | null
  }) => api.patch<ScanConfig>(`/projects/${slug}/scans/${scanId}`, data),

  del: (slug: string, scanId: string) =>
    api.del(`/projects/${slug}/scans/${scanId}`),

  run: (slug: string, scanId: string) =>
    api.post<ScanJob>(`/projects/${slug}/scans/${scanId}/run`, {}),

  applyEventGroups: (slug: string, scanId: string) =>
    api.post<ScanJob>(`/projects/${slug}/scans/${scanId}/event-groups/apply`, {}),

  replayMetrics: (slug: string, scanId: string, data: {
    time_from: string
    time_to: string
  }) => api.post<ScanJob>(`/projects/${slug}/scans/${scanId}/metrics/replay`, data),

  // Newest first. `limit` defaults to the backend's 50; the list page only needs
  // the head of each scan's history and passes a smaller one.
  listJobs: (slug: string, scanId: string, options: { limit?: number } = {}) =>
    api.get<ScanJob[]>(
      `/projects/${slug}/scans/${scanId}/jobs${options.limit ? `?limit=${options.limit}` : ''}`,
    ),

  // Every scan's latest job, exact failing streak and rows read in the last 24h,
  // in one request.
  activity: (slug: string) =>
    api.get<ScanActivityResponse>(`/projects/${slug}/scans/activity`),

  getJob: (slug: string, scanId: string, jobId: string) =>
    api.get<ScanJob>(`/projects/${slug}/scans/${scanId}/jobs/${jobId}`),

  cancelJob: (slug: string, scanId: string, jobId: string) =>
    api.post<ScanJob>(`/projects/${slug}/scans/${scanId}/jobs/${jobId}/cancel`, {}),
}
