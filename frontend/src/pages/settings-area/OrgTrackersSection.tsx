import { useEffect, useId, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { orgSettingsApi, type OrgTrackerDefaults } from '@/api/orgSettings'
import { useActiveOrg } from '@/components/active-org-context'
import { ErrorState } from '@/components/error-state'
import { Field, SCard, SHeader, SettingsSaveBar, TextInput } from '@/components/settings/kit'
import { useUnsavedChanges } from '@/components/settings/unsaved-changes'
import { ReadOnlyNotice, SectionSkeleton } from '@/components/states'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { orgTrackerDefaultsKey, trackerConfigRootKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import { OrgSourceBadge } from './org-settings/OrgSettingsPrimitives'
import {
  ORG_TRACKERS_PATH,
  buildTrackerUpdate,
  savedTrackerValue,
  jiraGroupWarning,
  secretConfigured,
  trackerDisplayValue,
  trackerDraftInvalid,
  trackerFieldError,
  trackerSource,
  trackerUpdateEmpty,
  withTrackerEdit,
  type TrackerDefaultKey,
  type TrackerDraft,
} from './org-settings/orgTrackersModel'

const UNSAVED_MESSAGE =
  'Tracker defaults you edited here have not been saved. Leaving this page drops them.'

/**
 * Organization › Trackers (F20 PR12): the Jira and Linear connection every
 * project of the organization uses for the fields its own tracker config
 * leaves empty. A project still turns its tracker on itself. Owners and admins
 * of the organization only (the area gates the route).
 */
export default function OrgTrackersSection() {
  const { slug } = useActiveOrg()
  return (
    <div>
      <SHeader
        title="Trackers"
        description="Jira and Linear defaults this organization's projects use for implementation tickets unless their own tracker settings say otherwise."
      />
      {slug ? (
        <OrgTrackersForm key={slug} org={slug} />
      ) : (
        <ReadOnlyNotice className="mb-5">You are not a member of any organization.</ReadOnlyNotice>
      )}
    </div>
  )
}

function OrgTrackersForm({ org }: { org: string }) {
  const qc = useQueryClient()
  const { registerUnsaved } = useUnsavedChanges()
  const [draft, setDraft] = useState<TrackerDraft>({})
  const query = useQuery({
    queryKey: orgTrackerDefaultsKey(org),
    queryFn: () => orgSettingsApi.getTrackers(org),
    meta: SILENT_ERROR_META,
  })
  const saveMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (next: TrackerDraft) => orgSettingsApi.updateTrackers(org, buildTrackerUpdate(next)),
    onSuccess: data => {
      qc.setQueryData(orgTrackerDefaultsKey(org), data)
      setDraft({})
      // Every project's tracker dialog reports what it inherits.
      void qc.invalidateQueries({ queryKey: trackerConfigRootKey() })
    },
  })

  const update = buildTrackerUpdate(draft)
  const dirty = !trackerUpdateEmpty(update)
  useEffect(() => {
    registerUnsaved(
      dirty
        ? { keptBy: () => false, message: UNSAVED_MESSAGE, dirtyPaths: [ORG_TRACKERS_PATH] }
        : null,
    )
    return () => registerUnsaved(null)
  }, [dirty, registerUnsaved])

  if (query.isError && !query.data) {
    return (
      <ErrorState
        title="Couldn't load the organization's tracker defaults"
        error={query.error}
        onRetry={() => {
          void query.refetch()
        }}
      />
    )
  }
  if (!query.data) return <SectionSkeleton variant="form" label="Loading tracker defaults…" />

  const defaults = query.data
  const fieldProps = {
    defaults,
    draft,
    setValue: (key: TrackerDefaultKey, value: string | null) =>
      setDraft(current => withTrackerEdit(defaults, current, key, value)),
  }
  const jiraWarning = jiraGroupWarning(defaults, draft)

  return (
    <div className="min-w-0 space-y-5">
      <p className="m-0 text-body-sm text-fg-tertiary">
        A project&rsquo;s own tracker settings win field by field; these fill in what it leaves
        empty. Each project still turns its tracker on itself. Organization: this
        organization&rsquo;s value. No badge: not set, so projects need their own.
      </p>
      <SettingsSaveBar
        note="Projects of this organization use the saved defaults from their next ticket on."
        error={saveMut.isError ? getErrorMessage(saveMut.error) : undefined}
        dirty={dirty}
        invalid={trackerDraftInvalid(draft)}
        pending={saveMut.isPending}
        onDiscard={() => setDraft({})}
        onSave={() => saveMut.mutate(draft)}
      />
      <SCard
        title="Jira"
        description="Site, account e-mail and API token are one unit: a project that sets any of them uses none of the organization's, so this token never reaches a site a project names."
      >
        {jiraWarning && (
          <p role="note" className="m-0 px-4 pt-2.5 text-caption text-(--warning)">
            {jiraWarning}
          </p>
        )}
        <TrackerTextField
          {...fieldProps}
          field="jira.base_url"
          label="Site URL"
          placeholder="e.g. https://acme.atlassian.net"
          hint="https only, and it must be a public address."
        />
        <TrackerTextField {...fieldProps} field="jira.auth_email" label="Account e-mail" placeholder="e.g. bot@acme.com" />
        <TrackerSecretField {...fieldProps} field="jira.api_token" label="API token" />
        <TrackerTextField
          {...fieldProps}
          field="jira.project_key"
          label="Default project key"
          placeholder="e.g. ENG"
          last
        />
      </SCard>
      <SCard title="Linear" description="Linear's address is fixed, so its key and team are inherited separately.">
        <TrackerSecretField {...fieldProps} field="linear.api_key" label="API key" />
        <TrackerTextField
          {...fieldProps}
          field="linear.team_id"
          label="Default team ID"
          placeholder="e.g. ENG or 9cfb482a-81e3-4154-b5b9-2c805e70a02d"
          last
        />
      </SCard>
    </div>
  )
}

type TrackerFieldProps = {
  defaults: OrgTrackerDefaults
  draft: TrackerDraft
  setValue: (key: TrackerDefaultKey, value: string | null) => void
  field: TrackerDefaultKey
  label: string
  last?: boolean
}

function ClearedHint({ onKeep }: { onKeep: () => void }) {
  return (
    <span>
      Saving removes this organization&rsquo;s value.{' '}
      <button type="button" className="font-medium text-accent hover:underline" onClick={onKeep}>
        Keep it
      </button>
    </span>
  )
}

function TrackerTextField({
  defaults,
  draft,
  setValue,
  field,
  label,
  placeholder,
  hint,
  last,
}: TrackerFieldProps & { placeholder?: string; hint?: string }) {
  const errorId = useId()
  const value = trackerDisplayValue(defaults, draft, field)
  const error = field in draft ? trackerFieldError(field, draft[field]) : null
  const cleared = draft[field] === null
  return (
    <Field
      label={label}
      labelRight={<OrgSourceBadge source={trackerSource(defaults, field)} />}
      hint={
        cleared ? (
          <ClearedHint onKeep={() => setValue(field, savedTrackerValue(defaults, field))} />
        ) : (
          hint
        )
      }
      last={last}
    >
      <TextInput
        value={value}
        onChange={next => setValue(field, next)}
        placeholder={placeholder}
        mono
        aria-invalid={error !== null}
        aria-describedby={error ? errorId : undefined}
      />
      {error && (
        <p id={errorId} className="mt-1 text-caption text-danger">
          {error}
        </p>
      )}
    </Field>
  )
}

function TrackerSecretField({ defaults, draft, setValue, field, label, last }: TrackerFieldProps) {
  const configured = secretConfigured(defaults, field)
  const cleared = draft[field] === null
  return (
    <Field
      label={label}
      labelRight={<OrgSourceBadge source={trackerSource(defaults, field)} />}
      hint={
        cleared ? (
          <ClearedHint onKeep={() => setValue(field, '')} />
        ) : configured ? (
          <span>
            Stored; leave blank to keep it.{' '}
            <button
              type="button"
              className="font-medium text-accent hover:underline"
              onClick={() => setValue(field, null)}
            >
              Remove it
            </button>
          </span>
        ) : undefined
      }
      last={last}
    >
      <TextInput
        type="password"
        autoComplete="off"
        value={cleared ? '' : trackerDisplayValue(defaults, draft, field)}
        onChange={next => setValue(field, next)}
        placeholder={configured ? 'Configured — leave blank to keep' : 'Not configured'}
      />
    </Field>
  )
}
