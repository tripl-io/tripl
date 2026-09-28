import { Suspense, useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { orgSettingsApi } from '@/api/orgSettings'
import { useActiveOrg } from '@/components/active-org-context'
import { ErrorState } from '@/components/error-state'
import { SHeader, SettingsSaveBar } from '@/components/settings/kit'
import { useUnsavedChanges } from '@/components/settings/unsaved-changes'
import { ReadOnlyNotice, SectionSkeleton } from '@/components/states'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import {
  aiStatusRootKey,
  authStatusKey,
  commandPaletteSearchRootKey,
  orgSettingsKey,
  photoLimitsKey,
  rowLimitDefaultsKey,
} from '@/lib/queryKeys'
import { lazyWithReload } from '@/lib/lazyWithReload'
import { getErrorMessage } from '@/lib/utils'
import { OrgAiFields } from './org-settings/OrgAiFields'
import { OrgEmailFields } from './org-settings/OrgEmailFields'
import { OrgLimitFields } from './org-settings/OrgLimitFields'
import { OrgSearchFields } from './org-settings/OrgSearchFields'
import { ORG_SOURCE_LEGEND } from './org-settings/OrgSettingsPrimitives'
import {
  EMPTY_DRAFTS,
  ORG_SECTIONS,
  ORG_SECTION_PATHS,
  SECTION_TITLES,
  buildOrgUpdate,
  draftInvalid,
  hasChanges,
  withEdit,
  type DraftValue,
  type OrgDraft,
  type OrgDrafts,
  type OrgSection,
} from './org-settings/orgSettingsModel'

// Its own chunk (F20 PR11): only the Storage page needs it.
const OrgStorageFields = lazyWithReload(() => import('./org-settings/OrgStorageFields'))

const DESCRIPTIONS: Record<OrgSection, string> = {
  email: "The mail relay this organization's alerts, digests and notifications go out through.",
  ai: "The AI provider this organization's explanations, suggestions and assistant use.",
  search: "The embedding model semantic search uses for this organization's events and plans.",
  storage: "Where this organization's event photos are stored, and what an upload may be.",
  limits: 'Row caps for the scans and metrics runs of this organization.',
}

const UNSAVED_MESSAGE =
  'Organization settings you edited here have not been saved. Leaving this page drops them.'

const ORG_PATHS: ReadonlySet<string> = new Set(Object.values(ORG_SECTION_PATHS))

/**
 * What the organization loses while the operator shares nothing with it.
 * Storage is always shared: an organization without its own uses the platform's.
 */
const FALLBACK_NONE_NOTES: Record<Exclude<OrgSection, 'limits' | 'storage'>, string> = {
  email: 'The operator shares no mail relay with organizations: until this organization sets its own, its email is off.',
  ai: 'The operator shares no AI provider with organizations: until this organization sets its own, its AI is off.',
  search:
    'The operator shares no embedding endpoint with organizations: until this organization sets its own, semantic search is off for it. Keyword search still works.',
}

/**
 * Organization › Email, AI, Search, Storage and Limits (F20 PR9-PR11): the organization's own
 * values, each shown with where it comes from (the organization, the
 * operator, the environment) and what it would inherit without its own.
 * Owners and admins of the organization only (the area gates the route).
 */
export default function OrgSettingsSection({ section }: { section: OrgSection }) {
  const { slug } = useActiveOrg()
  return (
    <div>
      <SHeader title={SECTION_TITLES[section]} description={DESCRIPTIONS[section]} />
      {slug ? (
        <OrgSettingsForm key={slug} org={slug} section={section} />
      ) : (
        <ReadOnlyNotice className="mb-5">You are not a member of any organization.</ReadOnlyNotice>
      )}
    </div>
  )
}

function OrgSettingsForm({ org, section }: { org: string; section: OrgSection }) {
  const qc = useQueryClient()
  const { registerUnsaved } = useUnsavedChanges()
  const [drafts, setDrafts] = useState<OrgDrafts>(EMPTY_DRAFTS)
  const query = useQuery({
    queryKey: orgSettingsKey(org),
    queryFn: () => orgSettingsApi.get(org),
    meta: SILENT_ERROR_META,
  })

  const saveMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: ({ target, draft }: { target: OrgSection; draft: OrgDraft }) =>
      orgSettingsApi.update(org, buildOrgUpdate(target, draft)),
    onSuccess: (data, { target }) => {
      qc.setQueryData(orgSettingsKey(org), data)
      // A save settles its own section only; another section's edits stand.
      setDrafts(current => ({ ...current, [target]: {} }))
      if (target === 'ai') void qc.invalidateQueries({ queryKey: aiStatusRootKey() })
      // A self-hosted default organization's relay IS the account relay.
      if (target === 'email') void qc.invalidateQueries({ queryKey: authStatusKey() })
      if (target === 'limits') void qc.invalidateQueries({ queryKey: rowLimitDefaultsKey() })
      if (target === 'storage') void qc.invalidateQueries({ queryKey: photoLimitsKey() })
      // A new vector space re-embeds this organization's projects: results
      // cached from the old one are stale.
      if (target === 'search') {
        void qc.invalidateQueries({ queryKey: aiStatusRootKey() })
        void qc.invalidateQueries({ queryKey: commandPaletteSearchRootKey() })
      }
    },
  })

  const dirtySections = ORG_SECTIONS.filter(key => Object.keys(drafts[key]).length > 0)
  const dirtyKey = dirtySections.map(key => ORG_SECTION_PATHS[key]).join('|')
  useEffect(() => {
    registerUnsaved(
      dirtyKey
        ? {
            // Moving between Email, AI, Search, Storage and Limits keeps this component, and so
            // the drafts; anything else unmounts it.
            keptBy: path => ORG_PATHS.has(path),
            message: UNSAVED_MESSAGE,
            dirtyPaths: dirtyKey.split('|'),
          }
        : null,
    )
    return () => registerUnsaved(null)
  }, [dirtyKey, registerUnsaved])

  if (query.isError && !query.data) {
    return (
      <ErrorState
        title="Couldn't load the organization's settings"
        error={query.error}
        onRetry={() => {
          void query.refetch()
        }}
      />
    )
  }
  if (!query.data) return <SectionSkeleton variant="form" label="Loading settings…" />

  const settings = query.data
  const draft = drafts[section]
  const update = buildOrgUpdate(section, draft)
  const dirty = hasChanges(update)
  const setDraft = (next: OrgDraft) => setDrafts(current => ({ ...current, [section]: next }))
  const setField = (field: string, value: DraftValue) =>
    setDrafts(current => ({
      ...current,
      [section]: withEdit(settings, current[section], section, field, value),
    }))
  const otherDirty = dirtySections.filter(key => key !== section)
  const fieldProps = { settings, draft, setField }

  return (
    <div className="min-w-0 space-y-5">
      {settings.scope === 'operator' ? (
        <p role="note" className="m-0 text-body-sm text-fg-tertiary">
          On this self-hosted instance these are the platform&rsquo;s own settings: account mail
          (sign-up, password reset, invitations) uses the same relay, and any other organization
          inherits them.
        </p>
      ) : (
        section !== 'limits' &&
        section !== 'storage' &&
        settings.operator_fallback === 'none' && (
          <p role="note" className="m-0 text-body-sm text-fg-tertiary">
            {FALLBACK_NONE_NOTES[section]}
          </p>
        )
      )}
      {section === 'email' && settings.scope === 'organization' && (
        <p className="m-0 text-body-sm text-fg-tertiary">
          Sign-up, password-reset and invitation mail always go through the platform&rsquo;s relay.
        </p>
      )}
      {section === 'search' && (
        <p className="m-0 text-body-sm text-fg-tertiary">
          Changing the model or endpoint re-embeds this organization&rsquo;s projects, and no one
          else&rsquo;s. Semantic results fill back in as the reindex runs; keyword search keeps
          working meanwhile.
        </p>
      )}
      <p className="m-0 text-body-sm text-fg-tertiary">{ORG_SOURCE_LEGEND}</p>
      <SettingsSaveBar
        note="Takes effect for this organization as soon as it is saved."
        warning={
          otherDirty.length > 0
            ? `Also unsaved: ${otherDirty.map(key => SECTION_TITLES[key]).join(', ')}. Save changes here saves ${SECTION_TITLES[section]} only.`
            : undefined
        }
        error={
          saveMut.isError && saveMut.variables?.target === section
            ? getErrorMessage(saveMut.error)
            : undefined
        }
        dirty={dirty}
        invalid={draftInvalid(settings, draft)}
        pending={saveMut.isPending}
        onDiscard={() => setDraft({})}
        onSave={() => saveMut.mutate({ target: section, draft })}
      />
      {section === 'email' && (
        <OrgEmailFields {...fieldProps} org={org} setDraft={setDraft} saving={saveMut.isPending} />
      )}
      {section === 'ai' && (
        <OrgAiFields {...fieldProps} org={org} setDraft={setDraft} saving={saveMut.isPending} />
      )}
      {section === 'search' && (
        <OrgSearchFields {...fieldProps} setDraft={setDraft} saving={saveMut.isPending} />
      )}
      {section === 'storage' && (
        <Suspense fallback={<SectionSkeleton variant="form" label="Loading storage…" />}>
          <OrgStorageFields {...fieldProps} setDraft={setDraft} saving={saveMut.isPending} />
        </Suspense>
      )}
      {section === 'limits' && <OrgLimitFields {...fieldProps} />}
    </div>
  )
}
