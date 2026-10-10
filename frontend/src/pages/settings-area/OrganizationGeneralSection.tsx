import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'

import { orgsApi, type OrgUpdate } from '@/api/orgs'
import { AUTH_QUERY_KEY } from '@/components/auth-context'
import { useActiveOrg } from '@/components/active-org-context'
import { ErrorState } from '@/components/error-state'
import {
  Field,
  InfoRow,
  NativeSelect,
  SCard,
  SettingsSaveBar,
  SHeader,
  TextInput,
} from '@/components/settings/kit'
import { useUnsavedChanges } from '@/components/settings/unsaved-changes'
import { ReadOnlyNotice, SectionSkeleton } from '@/components/states'
import { Button } from '@/components/ui/button'
import { useConfirm } from '@/hooks/useConfirm'
import { orgHomePath } from '@/lib/activeOrg'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { useCanCreateOrg, useOrgCreationIsEnterprise } from '@/lib/deploymentMode'
import { EnterpriseFeature } from '@/components/settings/EnterpriseFeature'
import { ORG_CREATION_TEASER } from '@/extensions/teasers'
import { useIsOrgOwner, useIsOwner } from '@/lib/permissions'
import { orgKey, orgRootKey, orgsKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import { PROJECT_ROLE_OPTIONS, type DefaultProjectRole } from '@/types'
import { DangerRow } from './ProjectDangerRows'

/** The organization slug's shape: the backend's `ORG_SLUG_PATTERN`. */
const ORG_SLUG_SHAPE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/
const CREATE_FORM_ID = 'create-organization-form'

const UNSAVED_MESSAGE =
  'Organization details you edited here have not been saved. Leaving this page drops them.'

/** What each default means, under the select. */
const DEFAULT_ACCESS_HINTS: Readonly<Record<DefaultProjectRole, string>> = {
  none: 'Projects are invite-only: a member sees a project once someone adds them in its Access settings.',
  viewer: 'Every member can open every project and read it. Writing needs an Editor role on the project.',
  editor: 'Every member can open and change every project.',
}

/**
 * Organization › Details, its General page (F20 PR7): the organization's name (an owner or admin
 * renames it), its slug (read-only: it is in every address, `/o/{slug}/…`, and
 * links already sent must keep working), its default access to projects (F20
 * PR15: what a member gets on a project with no row of theirs; an owner or
 * admin sets it) — name and default access saved together by the page's save
 * bar — "Create organization" (where the edition creates more than
 * one: a platform admin's, or anyone's in hosted mode; Community shows a
 * platform admin that it is Enterprise's), and the danger zone, where an owner deletes it. The default
 * organization cannot be deleted, so its danger zone is not drawn.
 */
export default function OrganizationGeneralSection() {
  const { slug } = useActiveOrg()
  const canCreateOrg = useCanCreateOrg()
  const orgCreationIsEnterprise = useOrgCreationIsEnterprise()

  return (
    <div>
      <SHeader
        title="Details"
        description="This organization's name, address and default access to projects. Its projects, data sources, API keys and members are its own."
      />
      {slug ? <OrganizationCards org={slug} /> : (
        <ReadOnlyNotice className="mb-5">You are not a member of any organization.</ReadOnlyNotice>
      )}
      {canCreateOrg && <CreateOrganizationCard />}
      {orgCreationIsEnterprise && (
        <div className="mt-5">
          <EnterpriseFeature teaser={ORG_CREATION_TEASER} />
        </div>
      )}
    </div>
  )
}

function OrganizationCards({ org }: { org: string }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const canEdit = useIsOwner()
  const isOrgOwner = useIsOrgOwner()
  const { confirm, dialog } = useConfirm()
  const { registerUnsaved } = useUnsavedChanges()
  const orgQuery = useQuery({ queryKey: orgKey(org), queryFn: () => orgsApi.get(org) })
  // One draft for both cards, saved by one bar: they PATCH the same
  // organization, and two footer Save buttons on one page were the only ones
  // left after Project › General moved to the bar.
  const [nameDraft, setNameDraft] = useState<string | null>(null)
  const [accessDraft, setAccessDraft] = useState<DefaultProjectRole | null>(null)

  const loaded = orgQuery.data
  const name = nameDraft ?? loaded?.name ?? ''
  const trimmed = name.trim()
  const access = accessDraft ?? loaded?.default_project_role ?? 'none'
  const nameDirty = !!loaded && trimmed !== loaded.name
  const accessDirty = !!loaded && access !== loaded.default_project_role
  const dirty = canEdit && (nameDirty || accessDirty)

  const saveMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (patch: OrgUpdate) => orgsApi.update(org, patch),
    onSuccess: (updated, patch) => {
      qc.setQueryData(orgKey(org), updated)
      setNameDraft(null)
      setAccessDraft(null)
      if (patch.name !== undefined) {
        // The switcher and every "which organization" label read the session.
        void qc.invalidateQueries({ queryKey: AUTH_QUERY_KEY })
        void qc.invalidateQueries({ queryKey: orgsKey() })
      }
      // Which projects a member sees, and what they may do in them, follow it.
      if (patch.default_project_role !== undefined) {
        void qc.invalidateQueries({ queryKey: orgRootKey(org) })
      }
    },
  })

  // The bar's edits arm the settings shell's leave guard, as Project ›
  // General's do; no other settings path keeps this draft.
  useEffect(() => {
    registerUnsaved(
      dirty
        ? { keptBy: () => false, message: UNSAVED_MESSAGE, dirtyPaths: ['organization/general'] }
        : null,
    )
    return () => registerUnsaved(null)
  }, [dirty, registerUnsaved])

  if (orgQuery.isPending) return <SectionSkeleton variant="form" label="Loading organization…" />
  if (orgQuery.isError) {
    return (
      <ErrorState
        title="Couldn't load the organization"
        error={orgQuery.error}
        onRetry={() => {
          void orgQuery.refetch()
        }}
      />
    )
  }
  const current = orgQuery.data

  const save = () => {
    if (!dirty || !trimmed || saveMut.isPending) return
    saveMut.mutate({
      ...(nameDirty ? { name: trimmed } : {}),
      ...(accessDirty ? { default_project_role: access } : {}),
    })
  }

  const discard = () => {
    setNameDraft(null)
    setAccessDraft(null)
    saveMut.reset()
  }

  const handleDelete = async () => {
    await confirm({
      title: `Delete ${current.name}?`,
      message:
        `Every project in ${current.name}, its data sources, API keys, settings and invitations are `
        + 'deleted, and every member loses access. This cannot be undone.',
      confirmLabel: 'Delete organization',
      variant: 'danger',
      requireText: current.slug,
      errorPrefix: 'Could not delete the organization',
      pendingLabel: 'Deleting…',
      action: async () => {
        await orgsApi.delete(current.slug, current.slug)
        await qc.invalidateQueries({ queryKey: AUTH_QUERY_KEY })
        void qc.invalidateQueries({ queryKey: orgsKey() })
        navigate('/', { replace: true })
      },
    })
  }

  return (
    <>
      {dialog}
      {canEdit && (
        // The one save model for a settings page, the bar Project › General
        // and the Platform pages use: Discard and Save changes for both cards.
        <SettingsSaveBar
          className="mb-4"
          note={
            saveMut.isSuccess && !dirty ? (
              <span className="text-success">Saved</span>
            ) : dirty ? (
              <span className="text-warning">Unsaved changes</span>
            ) : (
              'Saves the organization’s name and its default access to projects together.'
            )
          }
          error={saveMut.isError ? getErrorMessage(saveMut.error) : undefined}
          dirty={dirty}
          invalid={nameDirty && !trimmed}
          invalidMessage="Give the organization a name to save."
          pending={saveMut.isPending}
          onDiscard={discard}
          onSave={save}
        />
      )}
      {/* Named for what it holds: "General" inside a page called Details
          repeated Project › General's label for a different thing. */}
      <SCard title="Name and slug">
        <form
          noValidate
          onSubmit={(event) => {
            event.preventDefault()
            save()
          }}
        >
          {canEdit ? (
            <Field label="Name" htmlFor="org-name">
              <TextInput
                id="org-name"
                value={name}
                onChange={(next) => {
                  saveMut.reset()
                  setNameDraft(next)
                }}
                aria-required
              />
            </Field>
          ) : (
            <InfoRow label="Name" value={current.name} mono={false} />
          )}
          {/* Text, not a read-only input: a bordered box under an editable
              Name looked editable too, beside a hint saying it is not. */}
          <InfoRow label="Slug" value={current.slug} last />
          <p className="m-0 px-4 pb-3 text-caption text-fg-tertiary">
            Part of every address in this organization (/o/{current.slug}/…), so it cannot be changed.
          </p>
        </form>
      </SCard>

      <ProjectAccessCard
        value={access}
        canEdit={canEdit}
        onChange={(next) => {
          saveMut.reset()
          setAccessDraft(next)
        }}
      />

      {isOrgOwner && !current.is_default && (
        <SCard title="Danger zone" tone="danger">
          <DangerRow
            title="Delete organization"
            hint="Deletes every project, data source, API key and invitation of this organization. You type its slug to confirm."
            action={
              <Button type="button" size="sm" variant="danger" onClick={() => void handleDelete()}>
                Delete organization
              </Button>
            }
            last
          />
        </SCard>
      )}
    </>
  )
}

/**
 * Default access to projects (F20 PR15): what a member of the organization gets
 * on a project where they have no row. An owner or admin changes it (saved by
 * the page's bar with the name); everyone else reads it. Owners and admins see
 * every project whatever it says, and a project's own rows (Project › Access)
 * override it, "No access" included.
 */
function ProjectAccessCard({
  value,
  canEdit,
  onChange,
}: {
  value: DefaultProjectRole
  canEdit: boolean
  onChange: (next: DefaultProjectRole) => void
}) {
  const label = PROJECT_ROLE_OPTIONS.find((option) => option.value === value)?.label ?? value

  return (
    <SCard
      title="Default access to projects"
      description="What a member of this organization gets on a project they have not been added to. Owners and admins always see every project, and a project's Access settings can give someone more, less, or no access."
    >
      {canEdit ? (
        <Field
          label="Default access"
          htmlFor="org-default-project-role"
          hint={DEFAULT_ACCESS_HINTS[value]}
          last
        >
          <NativeSelect
            id="org-default-project-role"
            value={value}
            onChange={(next) => onChange(next as DefaultProjectRole)}
            options={PROJECT_ROLE_OPTIONS}
            width="fill"
          />
        </Field>
      ) : (
        <>
          <InfoRow label="Default access" value={label} mono={false} />
          <p className="m-0 px-4 pb-3 text-caption text-fg-tertiary">{DEFAULT_ACCESS_HINTS[value]}</p>
        </>
      )}
    </SCard>
  )
}

/**
 * "Create organization": a platform admin's, and in hosted mode every
 * signed-in account's (`POST /orgs` allows both). The creator becomes its owner and
 * lands in its (empty) workspace. `?create=1` — the switcher's menu item —
 * brings the form into view.
 */
function CreateOrganizationCard() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')
  const [slugError, setSlugError] = useState<string | null>(null)
  const [searchParams, setSearchParams] = useSearchParams()
  const wantsCreate = searchParams.get('create') === '1'

  useEffect(() => {
    if (!wantsCreate) return
    const input = document.getElementById('new-org-name')
    input?.scrollIntoView?.({ block: 'center' })
    input?.focus({ preventScroll: true })
    const next = new URLSearchParams(searchParams)
    next.delete('create')
    setSearchParams(next, { replace: true })
  }, [wantsCreate, searchParams, setSearchParams])

  const createMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => orgsApi.create({ name: name.trim(), slug: slug.trim() }),
    onSuccess: async (created) => {
      await qc.invalidateQueries({ queryKey: AUTH_QUERY_KEY })
      void qc.invalidateQueries({ queryKey: orgsKey() })
      navigate(orgHomePath(created.slug))
    },
  })

  const submit = () => {
    if (!name.trim() || !slug.trim() || createMut.isPending) return
    if (!ORG_SLUG_SHAPE.test(slug.trim())) {
      setSlugError('Lowercase letters, digits and single hyphens, like acme-labs.')
      return
    }
    createMut.mutate()
  }

  return (
    <SCard
      title="Create organization"
      description="You become its owner; invite its members from Invitations once you are in it."
      footer={
        <div className="flex w-full flex-wrap items-center justify-end gap-2">
          {createMut.isError && (
            <p role="alert" className="m-0 mr-auto text-body-sm text-destructive">
              {getErrorMessage(createMut.error)}
            </p>
          )}
          <Button
            type="submit"
            form={CREATE_FORM_ID}
            size="sm"
            disabled={!name.trim() || !slug.trim() || createMut.isPending}
          >
            {createMut.isPending ? 'Creating…' : 'Create organization'}
          </Button>
        </div>
      }
    >
      <form
        id={CREATE_FORM_ID}
        noValidate
        onSubmit={(event) => {
          event.preventDefault()
          submit()
        }}
      >
        <Field label="Name" htmlFor="new-org-name">
          <TextInput id="new-org-name" value={name} onChange={setName} placeholder="e.g. Acme Labs" />
        </Field>
        <Field
          label="Slug"
          htmlFor="new-org-slug"
          hint="Its address, /o/<slug>. It cannot be changed later."
          error={slugError}
          last
        >
          <TextInput
            id="new-org-slug"
            value={slug}
            mono
            onChange={(next) => {
              setSlug(next)
              setSlugError(null)
            }}
            placeholder="e.g. acme-labs"
          />
        </Field>
      </form>
    </SCard>
  )
}
