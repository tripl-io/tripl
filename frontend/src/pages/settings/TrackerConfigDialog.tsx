import { useEffect, useId, useState } from 'react'
import { FieldError } from '@/components/forms/FieldError'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'

import { ApiError } from '@/api/client'
import { trackerConfigApi } from '@/api/trackerConfig'
import { useAuth } from '@/components/auth-context'
import { ErrorState } from '@/components/error-state'
import { Skeleton } from '@/components/ui/skeleton'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { SegmentedControl, type SegmentedOption } from '@/components/ui/segmented-control'
import { Switch } from '@/components/ui/switch'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { trackerConfigKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type { ProjectTrackerConfig, ProjectTrackerConfigUpdate, TrackerType } from '@/types'
import { isOwner } from '@/lib/permissions'

const DEFAULT_ISSUE_TYPE = 'Task'

/** Decode a failed PATCH: 403 (non-owner) gets a plain-language line; 422 and
 * everything else falls back to the client's already-formatted message (which
 * expands FastAPI validation `detail` arrays into "base_url: …"-style text). */
function describeTrackerError(error: unknown): string {
  if (error instanceof ApiError && error.status === 403) {
    return 'Only project owners can change the tracker connection.'
  }
  return getErrorMessage(error)
}

type TrackerField = 'baseUrl' | 'projectKey' | 'authEmail' | 'teamId' | 'apiToken'

interface TrackerFormValues {
  trackerType: TrackerType
  enabled: boolean
  baseUrl: string
  projectKey: string
  authEmail: string
  teamId: string
  apiToken: string
}

const TRACKER_LABEL: Record<TrackerType, string> = { jira: 'Jira', linear: 'Linear' }

const TRACKER_OPTIONS = [
  { value: 'jira', label: 'Jira' },
  { value: 'linear', label: 'Linear' },
] as const satisfies ReadonlyArray<SegmentedOption<TrackerType>>

/** Jira calls its secret an API token, Linear an API key. */
function secretLabel(trackerType: TrackerType): string {
  return trackerType === 'linear' ? 'API key' : 'API token'
}

/** A saved type the UI does not know reads as Jira, the only tracker before #258. */
function savedTrackerType(config: ProjectTrackerConfig): TrackerType {
  return config.tracker_type === 'linear' ? 'linear' : 'jira'
}

// The backend's own rules (alerting_validation.py): `_validate_https_url` wants
// an https URL with a host and no whitespace, `_JIRA_PROJECT_KEY_RE` an
// uppercase key after it upper-cases the input, `validate_email_address` an
// address, and `_LINEAR_ID_RE` a Linear id. Checked here so a typo is named
// beside its field instead of coming back as one 422 line.
const JIRA_PROJECT_KEY_RE = /^[A-Z][A-Z0-9_]{1,31}$/
const LINEAR_ID_RE = /^[A-Za-z0-9_-]{1,64}$/
const REQUIRED_WHEN_ENABLED = 'Required while the tracker is enabled.'
const CANNOT_CLEAR = 'A saved value cannot be cleared; enter a new one.'

const JIRA_ENDPOINT_FIELDS = ['base_url', 'auth_email', 'api_token'] as const

/**
 * The fields this project would take from its organization's tracker defaults
 * after the save (F20 PR12). The response names what the SAVED config
 * inherits; the form adjusts it for what is typed: a switch of tracker drops
 * everything (the saved set is the other tracker's), and a project that types a
 * site, account or token of its own inherits none of the three — the Jira site,
 * account and token are one unit, so the organization's token never goes to a
 * site the project names.
 */
function inheritedAfterSave(values: TrackerFormValues, saved: ProjectTrackerConfig): ReadonlySet<string> {
  if (values.trackerType !== savedTrackerType(saved)) return new Set()
  const inherited = new Set(saved.inherited_fields ?? [])
  if (values.trackerType === 'jira') {
    const typedOwn =
      (values.baseUrl.trim() !== '' && values.baseUrl.trim() !== saved.base_url) ||
      (values.authEmail.trim() !== '' && values.authEmail.trim() !== saved.auth_email) ||
      values.apiToken.trim() !== ''
    const groupWasInherited = JIRA_ENDPOINT_FIELDS.some(field => inherited.has(field))
    if (typedOwn && groupWasInherited) for (const field of JIRA_ENDPOINT_FIELDS) inherited.delete(field)
  }
  return inherited
}

/**
 * What the form would save wrong, per field (PLAN-21).
 *
 * The backend validates every field it is SENT and rejects an empty one ("Jira
 * base_url is required"), but it does not require any of them to exist: a
 * tracker can be enabled with no project key and only fail later, in the merge
 * worker, where nobody sees it. So: a field that is filled must be valid; an
 * enabled tracker needs all of its own fields; a disabled one may stay
 * half-filled, because blank fields are simply not sent (see `trackerPatch`).
 * What cannot be done is blanking a field that has a saved value — the PATCH
 * has no way to clear it. Only the chosen tracker's fields are checked. A field
 * the organization's tracker defaults fill in is not required (F20 PR12).
 */
function trackerConfigErrors(
  values: TrackerFormValues,
  saved: ProjectTrackerConfig,
): Partial<Record<TrackerField, string>> {
  const errors: Partial<Record<TrackerField, string>> = {}
  const inherited = inheritedAfterSave(values, saved)
  const blank = (savedValue: string | null | undefined, field: string) =>
    (savedValue ?? '').trim() !== ''
      ? CANNOT_CLEAR
      : values.enabled && !inherited.has(field)
        ? REQUIRED_WHEN_ENABLED
        : null

  // One secret slot serves both trackers, and saving a switch makes the backend
  // DROP the stored secret and destination (project key / team) rather than
  // keep them parked — a Jira token must never reach Linear. So a switch needs
  // the new tracker's secret before it can be saved at all, enabled or not:
  // without it the save would leave the project with no credential.
  const switching = values.trackerType !== savedTrackerType(saved)
  if (switching && values.apiToken.trim() === '') {
    const wanted = `Enter the ${TRACKER_LABEL[values.trackerType]} ${secretLabel(values.trackerType)}`
    errors.apiToken = saved.api_token_set
      ? `${wanted}; the one stored is for ${TRACKER_LABEL[savedTrackerType(saved)]}.`
      : `${wanted} to switch trackers.`
  } else if (
    values.trackerType === 'jira' &&
    values.enabled &&
    values.apiToken.trim() === '' &&
    (saved.inherited_fields ?? []).includes('api_token') &&
    !inherited.has('api_token')
  ) {
    errors.apiToken =
      "Enter an API token: this project now names its own Jira site or account, and the organization's token is never sent there."
  }

  if (values.trackerType === 'linear') {
    const teamId = values.teamId.trim()
    if (teamId === '') {
      // After a switch the saved team is wiped by the save, so it cannot be
      // "kept"; only an enabled tracker then needs one.
      const message = switching ? (values.enabled ? REQUIRED_WHEN_ENABLED : null) : blank(saved.team_id, 'team_id')
      if (message) errors.teamId = message
    } else if (!LINEAR_ID_RE.test(teamId)) {
      errors.teamId = 'Use up to 64 letters, digits, dashes or underscores (the team id or key).'
    }
    return errors
  }

  const baseUrl = values.baseUrl.trim()
  if (baseUrl === '') {
    const message = blank(saved.base_url, 'base_url')
    if (message) errors.baseUrl = message
  } else {
    let parsed: URL | null
    try {
      parsed = /\s/.test(baseUrl) ? null : new URL(baseUrl)
    } catch {
      parsed = null
    }
    if (!parsed || parsed.protocol !== 'https:' || !parsed.hostname) {
      errors.baseUrl = 'Enter an https URL, such as https://acme.atlassian.net.'
    }
  }

  const projectKey = values.projectKey.trim()
  if (projectKey === '') {
    const message = blank(saved.project_key, 'project_key')
    if (message) errors.projectKey = message
  } else if (!JIRA_PROJECT_KEY_RE.test(projectKey.toUpperCase())) {
    errors.projectKey = 'Use 2–32 letters, digits or underscores, starting with a letter (e.g. ENG).'
  }

  const email = values.authEmail.trim()
  if (email === '') {
    const message = blank(saved.auth_email, 'auth_email')
    if (message) errors.authEmail = message
  } else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    errors.authEmail = 'Enter an email address.'
  }
  return errors
}

/**
 * The PATCH body: only what changed, and never an empty string. The backend
 * validates every field present and refuses a blank one, so sending the whole
 * form turned "save the issue type of a parked, half-filled connection" into a
 * 422 about a base URL nobody touched. Only the chosen tracker's fields travel.
 * Nothing of the old tracker survives a switch that matters: the backend clears
 * the stored secret and the project key / team id when `tracker_type` changes
 * (a Jira base URL and auth email stay in their columns but Linear never reads
 * them), so switching back later means re-entering the key and the secret.
 */
function trackerPatch(
  values: TrackerFormValues & { issueType: string },
  saved: ProjectTrackerConfig,
): ProjectTrackerConfigUpdate {
  const patch: ProjectTrackerConfigUpdate = {}
  if (values.trackerType !== savedTrackerType(saved)) patch.tracker_type = values.trackerType
  if (values.enabled !== saved.enabled) patch.enabled = values.enabled
  if (values.trackerType === 'linear') {
    const teamId = values.teamId.trim()
    if (teamId !== '' && teamId !== (saved.team_id ?? '')) patch.team_id = teamId
  } else {
    const text: Array<['base_url' | 'project_key' | 'auth_email' | 'issue_type', string]> = [
      ['base_url', values.baseUrl],
      ['project_key', values.projectKey],
      ['auth_email', values.authEmail],
      ['issue_type', values.issueType.trim() || DEFAULT_ISSUE_TYPE],
    ]
    for (const [key, raw] of text) {
      const value = raw.trim()
      if (value !== '' && value !== saved[key]) {
        patch[key] = value
      }
    }
  }
  // Only when the user actually typed one — otherwise omitted, so the stored
  // secret is preserved (an empty string would clear it).
  if (values.apiToken.trim() !== '') patch.api_token = values.apiToken
  return patch
}

interface TrackerConfigDialogProps {
  slug: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * Owner-gated dialog to configure the project's implementation-tracker (Jira
 * or Linear) connection. A sibling of the branch merge-policy dialog: same surface (the
 * branches settings tab), same Dialog/primitive styling.
 */
export function TrackerConfigDialog({ slug, open, onOpenChange }: TrackerConfigDialogProps) {
  const configQuery = useQuery({
    queryKey: trackerConfigKey(slug),
    queryFn: () => trackerConfigApi.get(slug),
    enabled: open,
    // Rendered in the dialog with a retry, instead of "Loading tracker…"
    // forever (PLAN-21).
    meta: SILENT_ERROR_META,
  })
  const config = configQuery.data

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Implementation tracker</DialogTitle>
        </DialogHeader>
        {config ? (
          <TrackerConfigForm slug={slug} config={config} onClose={() => onOpenChange(false)} />
        ) : configQuery.isError ? (
          <ErrorState
            compact
            className="my-4"
            title="Could not load the tracker connection"
            error={configQuery.error}
            onRetry={() => void configQuery.refetch()}
          />
        ) : (
          // The form's shape while it loads, not a sentence (#237 AU-43).
          <div role="status" className="space-y-4 py-4">
            <span className="sr-only">Loading tracker…</span>
            {[0, 1, 2].map(index => (
              <div key={index} className="space-y-1.5">
                <Skeleton className="h-3 w-24" />
                <Skeleton className="h-8 w-full" />
              </div>
            ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

interface TrackerConfigFormProps {
  slug: string
  config: ProjectTrackerConfig
  onClose: () => void
}

function TrackerConfigForm({ slug, config, onClose }: TrackerConfigFormProps) {
  const qc = useQueryClient()
  const { user } = useAuth()
  // PATCH is owner-only on the backend; mirror the merge-policy / general
  // settings gate so non-owners get a read-only view instead of a 403.
  const canEdit = isOwner(user?.role)

  const enabledId = useId()
  const baseUrlId = useId()
  const projectKeyId = useId()
  const authEmailId = useId()
  const apiTokenId = useId()
  const issueTypeId = useId()
  const teamIdId = useId()

  const [trackerType, setTrackerType] = useState<TrackerType>(savedTrackerType(config))
  const [enabled, setEnabled] = useState(config.enabled)
  const [baseUrl, setBaseUrl] = useState(config.base_url)
  const [projectKey, setProjectKey] = useState(config.project_key)
  const [authEmail, setAuthEmail] = useState(config.auth_email)
  const [teamId, setTeamId] = useState(config.team_id ?? '')
  const [issueType, setIssueType] = useState(config.issue_type || DEFAULT_ISSUE_TYPE)
  // The password field always starts empty: the raw secret is never returned,
  // so a blank field means "keep the stored one".
  const [apiToken, setApiToken] = useState('')

  const values: TrackerFormValues = {
    trackerType, enabled, baseUrl, projectKey, authEmail, teamId, apiToken,
  }
  const errors = trackerConfigErrors(values, config)
  const inherited = inheritedAfterSave(values, config)
  const fromOrg = (field: string, empty: boolean) =>
    empty && inherited.has(field) ? (
      <p className="text-body-sm text-fg-tertiary">From the organization&rsquo;s tracker defaults.</p>
    ) : null
  const invalid = Object.keys(errors).length > 0
  // Field errors show once the owner has tried to save, not while typing.
  const [attempted, setAttempted] = useState(false)
  const shown = attempted ? errors : {}
  const fieldProps = (field: TrackerField, errorId: string) =>
    shown[field]
      ? { 'aria-invalid': true as const, 'aria-describedby': errorId }
      : {}
  const baseUrlErrorId = useId()
  const projectKeyErrorId = useId()
  const authEmailErrorId = useId()
  const teamIdErrorId = useId()
  const apiTokenErrorId = useId()

  const label = TRACKER_LABEL[trackerType]
  const secret = secretLabel(trackerType)
  const isLinear = trackerType === 'linear'
  // A stored secret belongs to the saved tracker; after a switch it is not one
  // this tracker can use, so the field must not claim "stored — leave blank".
  const secretStored = config.api_token_set && trackerType === savedTrackerType(config)
  const savedLabel = TRACKER_LABEL[savedTrackerType(config)]
  const savedDestination = savedTrackerType(config) === 'linear' ? 'team' : 'project key'
  // Saving a switch wipes the old tracker's credential and destination
  // (project_tracker_config_service), so say so before the owner commits.
  const switchNotice = trackerType !== savedTrackerType(config)
    ? `Saving replaces the stored ${savedLabel} connection: its ${secretLabel(savedTrackerType(config))} and ${savedDestination} are removed. Enter the ${label} ${secret} below to save.`
    : null

  const saveMut = useMutation({
    // Rendered inline below the fields.
    meta: SILENT_ERROR_META,
    mutationFn: () => trackerConfigApi.update(slug, trackerPatch({ ...values, issueType }, config)),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: trackerConfigKey(slug) })
      // Clear the just-saved secret so the raw value never lingers in the DOM.
      setApiToken('')
      // Done means closed: left open under a green line with Cancel still on
      // offer, nobody could tell whether Cancel would undo the save (AU-38).
      toast.success(enabled ? `${label} tracker connected` : 'Tracker configuration saved')
      onClose()
    },
  })

  // The first control takes focus once the form is in, rather than the close
  // button, which is all the dialog holds while the config loads (AU-38).
  useEffect(() => {
    if (canEdit) document.getElementById(enabledId)?.focus()
  }, [canEdit, enabledId])

  const tokenPlaceholder = secretStored
    ? `${isLinear ? 'Key' : 'Token'} stored — leave blank to keep`
    : inherited.has('api_token')
      ? `The organization's ${secret} — leave blank to use it`
      : `Paste your ${label} ${secret}`

  return (
    <form
      noValidate
      onSubmit={(event) => {
        event.preventDefault()
        setAttempted(true)
        if (canEdit && !invalid) saveMut.mutate()
      }}
    >
      <div className="grid gap-4 py-4">
        <p className="text-body-sm text-fg-tertiary">
          When enabled, merging a branch opens one {label} ticket for its added/changed events;
          {isLinear ? ' completing' : ' closing'} the ticket marks those events implemented.
        </p>

        <div className="flex items-center justify-between gap-3">
          <div>
            <Label htmlFor={enabledId}>Enabled</Label>
            <p className="mt-1 text-body-sm text-fg-tertiary">
              Open implementation tickets when branches merge.
            </p>
          </div>
          <Switch
            id={enabledId}
            checked={enabled}
            onCheckedChange={setEnabled}
            disabled={!canEdit}
          />
        </div>

        <div className="flex items-center justify-between gap-3">
          <span className="text-body font-medium">Tracker</span>
          {canEdit ? (
            <SegmentedControl<TrackerType>
              aria-label="Tracker"
              value={trackerType}
              onChange={setTrackerType}
              options={TRACKER_OPTIONS}
              size="sm"
            />
          ) : (
            <span className="text-body">{label}</span>
          )}
        </div>

        {switchNotice && (
          <p
            role="note"
            data-testid="tracker-switch-notice"
            className="rounded-md border border-warning/40 bg-warning-soft px-3 py-2 text-body-sm text-fg"
          >
            {switchNotice}
          </p>
        )}

        {/* The connection belongs to the switch above: dimmed while the
            tracker is off, still editable so a connection can be parked
            half-filled (AU-38). */}
        <fieldset
          className={enabled ? 'grid gap-4' : 'grid gap-4 opacity-60 transition-opacity focus-within:opacity-100'}
        >
        <legend className="mb-3 text-body-sm font-semibold">Connection</legend>
        {isLinear ? (
          <div className="grid gap-2">
            <Label htmlFor={teamIdId}>Team ID</Label>
            <Input
              id={teamIdId}
              value={teamId}
              onChange={(event) => setTeamId(event.target.value)}
              placeholder="e.g. 9cfb482a-81e3-4154-b5b9-2c805e70a02d"
              disabled={!canEdit}
              {...fieldProps('teamId', teamIdErrorId)}
            />
            <FieldError id={teamIdErrorId} message={shown.teamId} />
            {fromOrg('team_id', teamId.trim() === '')}
            <p className="text-body-sm text-fg-tertiary">
              The Linear team the issues are created in.
            </p>
          </div>
        ) : (
          <>
            <div className="grid gap-2">
              <Label htmlFor={baseUrlId}>Base URL</Label>
              <Input
                id={baseUrlId}
                type="url"
                value={baseUrl}
                onChange={(event) => setBaseUrl(event.target.value)}
                placeholder="e.g. https://acme.atlassian.net"
                disabled={!canEdit}
                {...fieldProps('baseUrl', baseUrlErrorId)}
              />
              <FieldError id={baseUrlErrorId} message={shown.baseUrl} />
              {fromOrg('base_url', baseUrl.trim() === '')}
            </div>

            <div className="grid gap-2">
              <Label htmlFor={projectKeyId}>Project key</Label>
              <Input
                id={projectKeyId}
                value={projectKey}
                onChange={(event) => setProjectKey(event.target.value)}
                placeholder="e.g. ENG"
                disabled={!canEdit}
                {...fieldProps('projectKey', projectKeyErrorId)}
              />
              <FieldError id={projectKeyErrorId} message={shown.projectKey} />
              {fromOrg('project_key', projectKey.trim() === '')}
            </div>

            <div className="grid gap-2">
              <Label htmlFor={authEmailId}>Auth email</Label>
              <Input
                id={authEmailId}
                type="email"
                value={authEmail}
                onChange={(event) => setAuthEmail(event.target.value)}
                placeholder="e.g. you@acme.com"
                disabled={!canEdit}
                {...fieldProps('authEmail', authEmailErrorId)}
              />
              <FieldError id={authEmailErrorId} message={shown.authEmail} />
              {fromOrg('auth_email', authEmail.trim() === '')}
            </div>
          </>
        )}

        <div className="grid gap-2">
          <Label htmlFor={apiTokenId}>{isLinear ? 'API key' : 'API token'}</Label>
          <Input
            id={apiTokenId}
            type="password"
            autoComplete="off"
            value={apiToken}
            onChange={(event) => setApiToken(event.target.value)}
            placeholder={tokenPlaceholder}
            disabled={!canEdit}
            {...fieldProps('apiToken', apiTokenErrorId)}
          />
          <FieldError id={apiTokenErrorId} message={shown.apiToken} />
          <p className="text-body-sm text-fg-tertiary">
            {secretStored
              ? `A ${isLinear ? 'key' : 'token'} is stored. Leave this blank to keep it, or paste a new one to replace it.`
              : inherited.has('api_token') && apiToken.trim() === ''
                ? `The organization's ${secret} is used. Paste one to give this project its own.`
                : isLinear
                ? 'Create a personal API key in your Linear settings.'
                : 'Create an API token in your Jira account settings.'}
          </p>
        </div>

        {!isLinear && (
          <div className="grid gap-2">
            <Label htmlFor={issueTypeId}>Issue type</Label>
            <Input
              id={issueTypeId}
              value={issueType}
              onChange={(event) => setIssueType(event.target.value)}
              placeholder={DEFAULT_ISSUE_TYPE}
              disabled={!canEdit}
            />
          </div>
        )}
        </fieldset>

        {saveMut.isError && (
          <p className="text-body text-danger">
            {describeTrackerError(saveMut.error)}
          </p>
        )}
        {!canEdit && (
          <p className="text-body-sm text-fg-tertiary">
            Only project owners can edit the tracker connection.
          </p>
        )}
      </div>

      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>
          {canEdit ? 'Cancel' : 'Close'}
        </Button>
        {canEdit && (
          <Button type="submit" disabled={saveMut.isPending}>
            Save
          </Button>
        )}
      </DialogFooter>
    </form>
  )
}
