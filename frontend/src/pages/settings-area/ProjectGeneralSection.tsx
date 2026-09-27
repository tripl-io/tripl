import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Trash2, TriangleAlert, Wrench } from 'lucide-react'
import {
  projectsApi,
  type AnomalyResetCounts,
  type DetectionResetPeriod,
  type DriftResetCounts,
  type VariableRetirementCounts,
} from '@/api/projects'
import { searchApi } from '@/api/search'
import { useAuth } from '@/components/auth-context'
import { Button } from '@/components/ui/button'
import { useConfirm } from '@/hooks/useConfirm'
import { LEAVE_CONFIRMED, useUnsavedChanges } from '@/components/settings/unsaved-changes'
import {
  activeSignalsRootKey,
  commandPaletteSearchRootKey,
  distributionDriftsRootKey,
  eventsRootKey,
  eventTypeDriftsRootKey,
  eventTypesRootKey,
  metricsCatalogRootKey,
  monitoringSeriesRootKey,
  overviewRootKey,
  projectKey,
  projectRootKey,
  projectsKey,
  projectVariablesKey,
} from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import { countOf } from '@/lib/plural'
import {
  Field,
  InfoRow,
  SCard,
  NativeSelect,
  SHeader,
  SettingsSaveBar,
  TextArea,
  TextInput,
} from '@/components/settings/kit'
import { canManageProject, canWriteProject, isOwner } from '@/lib/permissions'
import { SLUG_ERROR, SLUG_HINT, isValidSlug } from '@/lib/slug'
import { forgetDemoLocalState } from '@/demo/demoLocalState'
import { deleteProjectConfirmation } from '@/lib/projectDeletion'
import { QueryErrorState, ReadOnlyNotice, SectionSkeleton } from '@/components/states'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import {
  RESET_PERIODS,
  SAVED_FEEDBACK_MS,
  timeZoneOptions,
  useTransientFlag,
} from './projectGeneralFields'
import {
  DANGER_ROW_CLASS,
  DANGER_ROW_CONTAINER_CLASS,
  DangerResetRow,
  DangerRetireVariablesRow,
  DangerRow,
} from './ProjectDangerRows'

const DAY_MS = 24 * 60 * 60 * 1000
const MAX_APP_VERSION_KEEP_RELEASES = 100

/**
 * The left affix on the Slug field — the one place in the product that shows a
 * reader what their project's address looks like.
 *
 * It was the bare literal `example.com/p/` (tripl-gex5), so every install welded
 * a stranger's domain onto a real slug and anyone who transcribed what they saw
 * got a dead link. The server cannot supply the answer either: it does not
 * reliably know its own public origin behind a proxy — the same reason
 * api/invitations.ts returns a path and lets the client join it to
 * window.location.origin — and the one value that records it, the instance's
 * app_base_url, is owner-only while this page is open to every editor. So take
 * the host the page was actually served from, which is by definition the one in
 * the reader's address bar.
 */
function projectUrlPrefix(): string {
  return `${window.location.host}/p/`
}

// Query prefixes refreshed after a reset so the cleared state is reflected
// everywhere the deleted detections (and their derived signals) surface.
const ANOMALY_INVALIDATE_KEYS: readonly (readonly string[])[] = [
  ['anomalies'],
  metricsCatalogRootKey(),
  overviewRootKey(),
  activeSignalsRootKey(),
  monitoringSeriesRootKey(),
  projectRootKey(),
  projectsKey(),
]
const DRIFT_INVALIDATE_KEYS: readonly (readonly string[])[] = [
  eventTypeDriftsRootKey(),
  distributionDriftsRootKey(),
  eventTypesRootKey(),
  eventsRootKey(),
  projectRootKey(),
  projectsKey(),
]

function resetPeriodPayload(value: string): DetectionResetPeriod {
  const option = RESET_PERIODS.find((period) => period.value === value)
  if (!option || option.days === null) return { before: null, after: null }
  return { before: new Date(Date.now() - option.days * DAY_MS).toISOString(), after: null }
}

function periodLabel(value: string): string {
  return (RESET_PERIODS.find((period) => period.value === value)?.label ?? 'All time').toLowerCase()
}

function summarizeAnomalyCounts(counts: AnomalyResetCounts): string {
  return `Cleared ${counts.metric_anomalies} anomalies and ${counts.metric_breakdown_anomalies} breakdown anomalies.`
}

// What a reset would delete, from its dry run (ST-39). Null — the preview
// failed — falls back to naming the loss without counting it.
function anomalyResetScope(counts: AnomalyResetCounts | null): string {
  if (!counts) return 'Permanently delete anomaly detections'
  return `Permanently delete ${countOf(counts.metric_anomalies, 'anomaly', 'anomalies')} and ${countOf(counts.metric_breakdown_anomalies, 'breakdown anomaly', 'breakdown anomalies')}`
}

// Said out loud rather than swallowed: a reader who saw counts on every other
// reset should not read the uncounted wording as "nothing to count".
function uncountedNote(counts: unknown): string {
  return counts ? '' : " The count couldn't be read first."
}

function driftResetScope(counts: DriftResetCounts | null): string {
  if (!counts) return 'Permanently delete schema and distribution drift detections'
  return `Permanently delete ${countOf(counts.schema_drifts, 'schema drift', 'schema drifts')} and ${countOf(counts.distribution_drifts, 'distribution drift', 'distribution drifts')}`
}

function summarizeDriftCounts(counts: DriftResetCounts): string {
  return `Cleared ${counts.schema_drifts} schema drifts and ${counts.distribution_drifts} distribution drifts.`
}

function summarizeRetirement(counts: VariableRetirementCounts, committed: boolean): string {
  const kept = [
    counts.kept_referenced && `${counts.kept_referenced} still referenced`,
    counts.kept_observed && `${counts.kept_observed} with observed values`,
    counts.kept_documented && `${counts.kept_documented} documented`,
    counts.kept_user_edited && `${counts.kept_user_edited} edited by hand`,
    counts.kept_excluded && `${counts.kept_excluded} excluded from scans`,
  ].filter(Boolean)
  const tail = kept.length ? ` Kept ${kept.join(', ')}.` : ''
  return committed
    ? `Retired ${counts.retired} of ${counts.scanned} variables.${tail}`
    : `${counts.retirable} of ${counts.scanned} variables can be retired.${tail}`
}

/**
 * Project · General. Identity (name / slug / description) and the search-index
 * rebuild reuse the real projectsApi + searchApi wiring lifted from GeneralTab.
 * A header cross-link jumps to Project operations (the in-app /p/:slug/settings
 * surfaces) so the two project-config halves stay reachable, and the danger zone
 * holds the owner-only resets and Delete. Archive and Transfer ownership rows
 * used to sit there as permanently disabled buttons with no backend behind them
 * and no word on why, which read as a permissions problem (WS-11); they return
 * when the features do.
 */
const UNSAVED_PROJECT_MESSAGE =
  'Project details you edited here have not been saved. Leaving this page drops them.'

export default function ProjectGeneralSection({
  slug,
  onSlugChanged,
}: {
  slug: string | undefined
  /**
   * Called with the new slug after a save renames the project, so whoever
   * chose `slug` rebinds to it. Without it the section kept requesting the old
   * address and fell over with "Failed to load project" (WS-8).
   */
  onSlugChanged?: (slug: string) => void
}) {
  if (!slug) {
    return (
      <div className="text-body text-fg-tertiary">
        Select a project to edit its settings.
      </div>
    )
  }
  return <ProjectGeneralBody slug={slug} onSlugChanged={onSlugChanged} />
}

function ProjectGeneralBody({
  slug,
  onSlugChanged,
}: {
  slug: string
  onSlugChanged?: (slug: string) => void
}) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const { user } = useAuth()
  const { confirm, dialog } = useConfirm()

  const projectQuery = useQuery({ queryKey: projectKey(slug), queryFn: () => projectsApi.get(slug) })

  const [name, setName] = useState('')
  const [slugDraft, setSlugDraft] = useState('')
  const [description, setDescription] = useState('')
  const [appVersionKeepReleases, setAppVersionKeepReleases] = useState('')
  const [timezone, setTimezone] = useState('UTC')
  const [hydratedFor, setHydratedFor] = useState<string | null>(null)
  const [saved, markSaved, clearSaved] = useTransientFlag(SAVED_FEEDBACK_MS)

  if (projectQuery.data && hydratedFor !== projectQuery.data.id) {
    setName(projectQuery.data.name)
    setSlugDraft(projectQuery.data.slug)
    setDescription(projectQuery.data.description ?? '')
    setAppVersionKeepReleases(String(projectQuery.data.app_version_keep_releases))
    setTimezone(projectQuery.data.timezone ?? 'UTC')
    setHydratedFor(projectQuery.data.id)
  }

  // One draft, one save (ST-3): the details and the version policy used to
  // have a Save button each, with no Discard and no rail dot, beside the
  // instance pages' single sticky bar. The PATCH carries only the part that
  // changed, so a version-policy edit does not resend the details.
  const saveMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (payload: Parameters<typeof projectsApi.update>[1]) => projectsApi.update(slug, payload),
    onMutate: clearSaved,
    onSuccess: (project) => {
      if (project.slug !== slug) {
        // The old address is gone. Drop its cache entry before the refresh
        // below, or the refresh asks the server for it and gets a 404.
        qc.removeQueries({ queryKey: projectKey(slug) })
        try {
          localStorage.setItem('tripl-last-project-slug', project.slug)
        } catch {
          /* ignore */
        }
        onSlugChanged?.(project.slug)
      }
      // The saved project is the answer: with it in the cache the form is
      // pristine at once, so "Saved" shows without waiting for a refetch.
      qc.setQueryData(projectKey(project.slug), project)
      qc.invalidateQueries({ queryKey: projectsKey() })
      qc.invalidateQueries({ queryKey: projectRootKey() })
      markSaved()
    },
  })
  const reindexMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => searchApi.reindex(slug),
    onSuccess: () => qc.invalidateQueries({ queryKey: commandPaletteSearchRootKey() }),
  })
  // The delete dialog renders a failure in place (WS-9), so no toast as well.
  const deleteMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => projectsApi.del(slug),
    onSuccess: () => {
      // A demo's tour, scenario and welcome state is keyed by slug and would
      // otherwise outlive it (DEMO-17); harmless for any other project.
      forgetDemoLocalState(slug)
      qc.invalidateQueries({ queryKey: projectsKey() })
      // The project is gone, and any draft of its details with it: nothing for
      // the unsaved-changes guard to ask about.
      navigate('/', { replace: true, state: LEAVE_CONFIRMED })
      qc.removeQueries({ queryKey: projectKey(slug) })
    },
  })

  const [anomaliesPeriod, setAnomaliesPeriod] = useState('30d')
  const [driftsPeriod, setDriftsPeriod] = useState('30d')

  // The reset, preview and retire mutations below each render their error in
  // their own danger-zone row (`feedback`), so the global toast stays quiet.
  const resetAnomaliesMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (period: DetectionResetPeriod) => projectsApi.resetAnomalies(slug, period),
    onSuccess: () => {
      for (const key of ANOMALY_INVALIDATE_KEYS) qc.invalidateQueries({ queryKey: key })
    },
  })
  const resetDriftsMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (period: DetectionResetPeriod) => projectsApi.resetDrifts(slug, period),
    onSuccess: () => {
      for (const key of DRIFT_INVALIDATE_KEYS) qc.invalidateQueries({ queryKey: key })
    },
  })

  // Preview and apply are separate mutations on purpose. Sharing one would make
  // the success banner ambiguous — "1284 can be retired" and "retired 1284" are
  // the same shape and very much not the same event.
  const [retirementPreview, setRetirementPreview] = useState<VariableRetirementCounts>()
  const previewRetirementMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => projectsApi.retireUnusedVariables(slug, { dry_run: true }),
    onSuccess: setRetirementPreview,
  })
  const retireVariablesMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => projectsApi.retireUnusedVariables(slug, { dry_run: false }),
    onSuccess: () => {
      setRetirementPreview(undefined)
      // Across every branch: the pass resolves the branch server-side, so this
      // button does not know which one the settings table is showing.
      qc.invalidateQueries({ queryKey: projectVariablesKey(slug) })
    },
  })

  const handleRetireVariables = async () => {
    const retirable = retirementPreview?.retirable ?? 0
    const ok = await confirm({
      title: 'Retire unused variables',
      message: `Permanently delete ${retirable} variable${retirable === 1 ? '' : 's'} that no event field value references. Variables you edited, documented, excluded from scans, or that carry observed values or drift are not touched. This cannot be undone.`,
      confirmLabel: 'Retire variables',
      variant: 'danger',
    })
    if (ok) retireVariablesMut.mutate()
  }

  // Each reset counts first (a dry run over the SAME window the delete then
  // uses), so the confirm says what goes rather than only that something does
  // (ST-39). A failed count does not block the reset, but the dialog says so.
  const handleResetAnomalies = async () => {
    const period = resetPeriodPayload(anomaliesPeriod)
    const preview = await projectsApi
      .resetAnomalies(slug, { ...period, dry_run: true })
      .catch(() => null)
    const ok = await confirm({
      title: 'Reset anomalies',
      message: `${anomalyResetScope(preview)} (${periodLabel(anomaliesPeriod)}) across this entire project — every scan and catalog metric. Monitoring signals derived from them are cleared too.${uncountedNote(preview)} This cannot be undone.`,
      confirmLabel: 'Reset anomalies',
      variant: 'danger',
    })
    if (ok) resetAnomaliesMut.mutate(period)
  }

  const handleResetDrifts = async () => {
    const period = resetPeriodPayload(driftsPeriod)
    const preview = await projectsApi
      .resetDrifts(slug, { ...period, dry_run: true })
      .catch(() => null)
    const ok = await confirm({
      title: 'Reset drifts',
      message: `${driftResetScope(preview)} (${periodLabel(driftsPeriod)}) across this entire project.${uncountedNote(preview)} This cannot be undone.`,
      confirmLabel: 'Reset drifts',
      variant: 'danger',
    })
    if (ok) resetDriftsMut.mutate(period)
  }

  const handleDelete = () => {
    // A failure from an earlier attempt belongs to that attempt.
    deleteMut.reset()
    void confirm(
      deleteProjectConfirmation(
        { name: projectQuery.data?.name ?? slug, slug },
        () => deleteMut.mutateAsync(),
      ),
    )
  }

  const slugError = isValidSlug(slugDraft) ? null : SLUG_ERROR
  // The select offers only zones the browser knows plus the stored value, so
  // there is nothing to refuse here: a stored zone the browser does not list
  // was accepted by the server and is only flagged "(not recognised)".
  const timezoneOptions = useMemo(() => timeZoneOptions(timezone), [timezone])

  const isPristine =
    !!projectQuery.data &&
    name === projectQuery.data.name &&
    slugDraft === projectQuery.data.slug &&
    description === (projectQuery.data.description ?? '') &&
    timezone === (projectQuery.data.timezone ?? 'UTC')
  // PATCH /projects/{slug} takes an editor AND the project's creator or an
  // owner (`_require_project_manager`); an editor on someone else's project
  // gets the same read-only form a viewer does, with a line saying why.
  const canEdit = canManageProject(user, projectQuery.data)
  // Reindex needs project mutation access, not project management: the backend
  // grants it to editors on shared projects too. The project now says whether
  // this user may mutate it (`can_mutate`), which canWriteProject reads — so a
  // demo that belongs to someone else shows the button disabled instead of
  // offering a click that can only be refused.
  const canReindex = canWriteProject(user, projectQuery.data)
  const canDelete = isOwner(user?.role)
  const appVersionKeepReleasesNumber = Number(appVersionKeepReleases)
  const versionPolicyInvalid =
    !Number.isInteger(appVersionKeepReleasesNumber) ||
    appVersionKeepReleasesNumber < 1 ||
    appVersionKeepReleasesNumber > MAX_APP_VERSION_KEEP_RELEASES
  const versionPolicyPristine =
    appVersionKeepReleasesNumber === projectQuery.data?.app_version_keep_releases

  // Either card's unsaved edits arm the settings shell's leave guard: the rail,
  // "View project", Back and reload all used to drop them silently (WS-13).
  // A read-only form is never dirty. No settings path keeps this draft: the
  // section is the only one that renders it. `dirtyPaths` puts the rail's
  // unsaved dot on General, as the instance pages have it (ST-3).
  const { registerUnsaved } = useUnsavedChanges()
  const dirty = canEdit && !!projectQuery.data && (!isPristine || !versionPolicyPristine)
  useEffect(() => {
    registerUnsaved(
      dirty
        ? { keptBy: () => false, message: UNSAVED_PROJECT_MESSAGE, dirtyPaths: ['project/general'] }
        : null,
    )
    return () => registerUnsaved(null)
  }, [dirty, registerUnsaved])

  const discard = () => {
    const project = projectQuery.data
    if (!project) return
    setName(project.name)
    setSlugDraft(project.slug)
    setDescription(project.description ?? '')
    setTimezone(project.timezone ?? 'UTC')
    setAppVersionKeepReleases(String(project.app_version_keep_releases))
    saveMut.reset()
  }

  const save = () => {
    if (!canEdit || slugError || versionPolicyInvalid) return
    saveMut.mutate({
      ...(isPristine ? {} : { name, slug: slugDraft, description, timezone }),
      ...(versionPolicyPristine ? {} : { app_version_keep_releases: appVersionKeepReleasesNumber }),
    })
  }

  return (
    <div>
      {dialog}
      <SHeader
        title="General"
        description="Identity and configuration for this tracking plan. These apply to everyone working in the project."
        actions={
          <>
            <Button
              variant="outline"
              size="sm"
              onClick={() => navigate(`/p/${slug}/event-types`)}
            >
              {/* Named for what it opens: event types, meta fields, alerting…
                  "Project operations" described none of them (#238 ST-5). */}
              Tracking plan &amp; alerting
            </Button>
            <Button variant="outline" size="sm" onClick={() => navigate(`/p/${slug}/events`)}>
              View project
            </Button>
          </>
        }
      />

      {/* The two cards' shape while the project loads, and a retry (or the
          way back, for a project that no longer exists) when it fails
          (#237 ST-35 / SH-33). */}
      {projectQuery.isLoading && <SectionSkeleton variant="form" rows={5} label="Loading project…" />}
      {projectQuery.isError && (
        <QueryErrorState
          error={projectQuery.error}
          title="Could not load this project"
          onRetry={() => void projectQuery.refetch()}
          notFound={{ title: 'Project not found', back: { to: '/workspace', label: 'Back to all projects' } }}
        />
      )}

      {projectQuery.data && (
        <>
          {!canEdit && (
            <ReadOnlyNotice className="mb-4">
              {/* A viewer of the project gets the notice's own viewer wording. */}
              {projectQuery.data.my_role !== 'viewer'
                ? 'Read-only: only the project’s creator or an owner can change its details and version policy.'
                : undefined}
            </ReadOnlyNotice>
          )}
          {canEdit && (
            // The one save model for a settings page (ST-3), the bar the
            // instance pages use: Discard and Save changes for both cards.
            // A failed save is red and announced; a 403 or a 409 for a taken
            // slug used to render in hint grey, where it read like advice
            // (WS-15). An edited form says so in amber, so the lit Save is
            // not the only difference from a pristine one (ST-2).
            <SettingsSaveBar
              className="mb-4"
              note={
                saved && !dirty ? (
                  <span className="text-success">Saved</span>
                ) : dirty ? (
                  <span className="text-warning">Unsaved changes</span>
                ) : (
                  'Saves the project details and the version policy together.'
                )
              }
              error={saveMut.isError ? getErrorMessage(saveMut.error) : undefined}
              dirty={dirty}
              invalid={!!slugError || versionPolicyInvalid}
              pending={saveMut.isPending}
              onDiscard={discard}
              onSave={save}
            />
          )}
          {canEdit ? (
            <>
              <SCard title="Project details">
                <Field label="Name" hint="Shown across the workspace and in the project switcher." htmlFor="proj-name">
                  <TextInput id="proj-name" value={name} onChange={setName} />
                </Field>
                {/* A bad slug is an error, red and tied to the input, not the
                    hint turning into a rule in the same grey (ST-2). Shown once
                    there is something typed: an emptied box is not yet wrong. */}
                <Field
                  label="Slug"
                  hint={`${SLUG_HINT} Changing it rewrites project URLs.`}
                  error={slugError && slugDraft.length > 0 ? slugError : undefined}
                  htmlFor="proj-slug"
                >
                  <TextInput
                    id="proj-slug"
                    value={slugDraft}
                    onChange={setSlugDraft}
                    mono
                    prefix={projectUrlPrefix()}
                  />
                </Field>
                {/* Stacked, and growing with its text: two fixed rows cut a
                    seeded description mid-line on a phone (ST-37). */}
                <Field label="Description" htmlFor="proj-desc" stacked>
                  <TextArea id="proj-desc" value={description} onChange={setDescription} rows={3} autoGrow />
                </Field>
                <Field
                  label="Timezone"
                  htmlFor="proj-timezone"
                  hint="The clock alert delivery schedules are read in. Type to jump, e.g. Europe/Moscow."
                  last
                >
                  <NativeSelect
                    id="proj-timezone"
                    value={timezone}
                    onChange={setTimezone}
                    options={timezoneOptions}
                    width="fill"
                  />
                </Field>
              </SCard>

              <SCard
                title="Version monitoring"
                description="One release-retention policy for event monitoring and catalog metrics."
              >
                <Field
                  label="Releases to keep"
                  hint="Older releases are combined into Other across every app-version chart in this project."
                  error={
                    versionPolicyInvalid
                      ? `Enter a whole number from 1 to ${MAX_APP_VERSION_KEEP_RELEASES}.`
                      : undefined
                  }
                  htmlFor="app-version-keep-releases"
                  last
                >
                  <TextInput
                    id="app-version-keep-releases"
                    type="number"
                    value={appVersionKeepReleases}
                    onChange={setAppVersionKeepReleases}
                  />
                </Field>
              </SCard>
            </>
          ) : (
            // Read-only: the values as text, not a form of dashed dead inputs
            // under a notice that already says nothing here can change (ST-18).
            <>
              <SCard title="Project details">
                <InfoRow label="Name" value={projectQuery.data.name} mono={false} />
                <InfoRow label="Slug" value={projectQuery.data.slug} />
                <InfoRow label="Description" value={projectQuery.data.description || '—'} mono={false} />
                <InfoRow label="Timezone" value={projectQuery.data.timezone ?? 'UTC'} mono={false} last />
              </SCard>
              <SCard
                title="Version monitoring"
                description="One release-retention policy for event monitoring and catalog metrics."
              >
                <InfoRow
                  label="Releases to keep"
                  value={projectQuery.data.app_version_keep_releases}
                  mono={false}
                  last
                />
              </SCard>
            </>
          )}

          {/* Routine upkeep, apart from the one irreversible act below: three
              resets beside "Delete project" diluted the danger signal (ST-38).
              Rows the reader cannot run are left out, not shown disabled
              (ST-18); with none left the card goes too. */}
          {(canReindex || canDelete) && (
            <SCard
              title="Maintenance"
              icon={<Wrench className="size-4" />}
              description={canDelete ? 'Resets delete detections for good; the index rebuild only refreshes search.' : undefined}
            >
              {canReindex && (
                <div
                  className={DANGER_ROW_CONTAINER_CLASS}
                  style={{ borderBottom: canDelete ? '1px solid var(--border-subtle)' : 'none' }}
                >
                  <div className={DANGER_ROW_CLASS}>
                    <div className="min-w-0 flex-1">
                      <div className="text-body font-medium">Rebuild search index</div>
                      <div className="mt-[3px] text-body-sm leading-[1.45] text-fg-tertiary">
                        Rebuild project search when existing events, descriptions, or fields do not appear
                        in global search.
                      </div>
                      {reindexMut.isSuccess && (
                        <div className="mt-2 text-body-sm text-success">
                          Indexed {reindexMut.data.documents_indexed} documents
                          {reindexMut.data.embeddings_scheduled ? '; embeddings queued.' : '.'}
                        </div>
                      )}
                      {reindexMut.isError && (
                        <div className="mt-2 text-body-sm text-danger">
                          {getErrorMessage(reindexMut.error)}
                        </div>
                      )}
                    </div>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => reindexMut.mutate()}
                      disabled={reindexMut.isPending}
                    >
                      <RefreshCw className={reindexMut.isPending ? 'h-3 w-3 animate-spin' : 'h-3 w-3'} />
                      {reindexMut.isPending ? 'Rebuilding…' : 'Rebuild index'}
                    </Button>
                  </div>
                </div>
              )}
              {canDelete && (
                <>
                  <DangerResetRow
                    title="Reset anomalies"
                    hint="Delete anomaly detections (and the signals derived from them) across the whole project for the chosen period."
                    buttonLabel="Reset anomalies"
                    period={anomaliesPeriod}
                    onPeriodChange={setAnomaliesPeriod}
                    onReset={() => {
                      void handleResetAnomalies()
                    }}
                    busy={resetAnomaliesMut.isPending}
                    feedback={
                      resetAnomaliesMut.isSuccess ? (
                        <div className="mt-2 text-body-sm text-success">
                          {summarizeAnomalyCounts(resetAnomaliesMut.data)}
                        </div>
                      ) : resetAnomaliesMut.isError ? (
                        <div className="mt-2 text-body-sm text-danger">
                          {getErrorMessage(resetAnomaliesMut.error)}
                        </div>
                      ) : null
                    }
                  />
                  <DangerResetRow
                    title="Reset drifts"
                    hint="Delete schema and distribution drift detections across the whole project for the chosen period."
                    buttonLabel="Reset drifts"
                    period={driftsPeriod}
                    onPeriodChange={setDriftsPeriod}
                    onReset={() => {
                      void handleResetDrifts()
                    }}
                    busy={resetDriftsMut.isPending}
                    feedback={
                      resetDriftsMut.isSuccess ? (
                        <div className="mt-2 text-body-sm text-success">
                          {summarizeDriftCounts(resetDriftsMut.data)}
                        </div>
                      ) : resetDriftsMut.isError ? (
                        <div className="mt-2 text-body-sm text-danger">
                          {getErrorMessage(resetDriftsMut.error)}
                        </div>
                      ) : null
                    }
                  />
                  <DangerRetireVariablesRow
                    onPreview={() => {
                      // The row shows one outcome, and an earlier retire's
                      // result outranks the preview's: drop it, or a failed
                      // preview would have nowhere to show.
                      retireVariablesMut.reset()
                      previewRetirementMut.mutate()
                    }}
                    onRetire={() => {
                      void handleRetireVariables()
                    }}
                    busy={previewRetirementMut.isPending || retireVariablesMut.isPending}
                    preview={retirementPreview}
                    feedback={
                      retireVariablesMut.isSuccess ? (
                        <div className="mt-2 text-body-sm text-success">
                          {summarizeRetirement(retireVariablesMut.data, true)}
                        </div>
                      ) : retireVariablesMut.isError ? (
                        <div className="mt-2 text-body-sm text-danger">
                          {getErrorMessage(retireVariablesMut.error)}
                        </div>
                      ) : previewRetirementMut.isError ? (
                        <div className="mt-2 text-body-sm text-danger">
                          {getErrorMessage(previewRetirementMut.error)}
                        </div>
                      ) : retirementPreview ? (
                        <div className="mt-2 text-body-sm text-fg-tertiary">
                          {summarizeRetirement(retirementPreview, false)}
                        </div>
                      ) : null
                    }
                    last
                  />
                </>
              )}
            </SCard>
          )}

          {/* Owner-only, so a non-owner is not shown a card of a button they
              can never press. */}
          {canDelete && (
            <SCard title="Danger zone" tone="danger" icon={<TriangleAlert className="size-4" />}>
              <DangerRow
                title="Delete project"
                hint="Permanently remove the plan, history and all ingested events. Cannot be undone."
                last
                action={
                  // Bare red in a row; the solid red is the confirm's (DS-20).
                  <Button
                    variant="danger"
                    size="sm"
                    disabled={deleteMut.isPending}
                    onClick={handleDelete}
                  >
                    <Trash2 className="h-3 w-3" />
                    {deleteMut.isPending ? 'Deleting…' : 'Delete project'}
                  </Button>
                }
              />
            </SCard>
          )}
        </>
      )}
    </div>
  )
}
