import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'

import { orgsApi } from '@/api/orgs'
import { AUTH_QUERY_KEY } from '@/components/auth-context'
import { useActiveOrg } from '@/components/active-org-context'
import { ErrorState } from '@/components/error-state'
import { Field, InfoRow, NativeSelect, SCard, SHeader, TextInput } from '@/components/settings/kit'
import { ReadOnlyNotice, SectionSkeleton } from '@/components/states'
import { Button } from '@/components/ui/button'
import { useConfirm } from '@/hooks/useConfirm'
import { orgHomePath } from '@/lib/activeOrg'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { useCanCreateOrg } from '@/lib/deploymentMode'
import { useIsOrgOwner, useIsOwner } from '@/lib/permissions'
import { orgKey, orgRootKey, orgsKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import { PROJECT_ROLE_OPTIONS, type DefaultProjectRole } from '@/types'
import { DangerRow } from './ProjectDangerRows'

/** The organization slug's shape: the backend's `ORG_SLUG_PATTERN`. */
const ORG_SLUG_SHAPE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/
const CREATE_FORM_ID = 'create-organization-form'
const RENAME_FORM_ID = 'rename-organization-form'
const PROJECT_ACCESS_FORM_ID = 'organization-project-access-form'

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
 * admin sets it), "Create organization" (a platform
 * admin's, or anyone's in hosted mode), and the danger zone, where an owner deletes it. The default
 * organization cannot be deleted, so its danger zone is not drawn.
 */
export default function OrganizationGeneralSection() {
  const { slug } = useActiveOrg()
  const canCreateOrg = useCanCreateOrg()

  return (
    <div>
      <SHeader
        title="Details"
        description="The organization this workspace belongs to. Its projects, data sources, API keys and members are its own."
      />
      {slug ? <OrganizationCards org={slug} /> : (
        <ReadOnlyNotice className="mb-5">You are not a member of any organization.</ReadOnlyNotice>
      )}
      {canCreateOrg && <CreateOrganizationCard />}
    </div>
  )
}

function OrganizationCards({ org }: { org: string }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const canRename = useIsOwner()
  const isOrgOwner = useIsOrgOwner()
  const { confirm, dialog } = useConfirm()
  const orgQuery = useQuery({ queryKey: orgKey(org), queryFn: () => orgsApi.get(org) })
  const [draft, setDraft] = useState<string | null>(null)
  const name = draft ?? orgQuery.data?.name ?? ''

  const renameMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (next: string) => orgsApi.update(org, { name: next }),
    onSuccess: (renamed) => {
      qc.setQueryData(orgKey(org), renamed)
      setDraft(null)
      // The switcher and every "which organization" label read the session.
      void qc.invalidateQueries({ queryKey: AUTH_QUERY_KEY })
      void qc.invalidateQueries({ queryKey: orgsKey() })
    },
  })

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
  const trimmed = name.trim()
  const dirty = trimmed !== current.name

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
      <SCard
        title="General"
        footer={
          canRename ? (
            <div className="flex w-full flex-wrap items-center justify-end gap-2">
              {renameMut.isError && (
                <p role="alert" className="m-0 mr-auto text-body-sm text-destructive">
                  {getErrorMessage(renameMut.error)}
                </p>
              )}
              {renameMut.isSuccess && !dirty && (
                <p role="status" className="m-0 mr-auto text-body-sm text-success">Saved</p>
              )}
              <Button
                type="submit"
                form={RENAME_FORM_ID}
                size="sm"
                disabled={!dirty || !trimmed || renameMut.isPending}
              >
                {renameMut.isPending ? 'Saving…' : 'Save'}
              </Button>
            </div>
          ) : undefined
        }
      >
        <form
          id={RENAME_FORM_ID}
          noValidate
          onSubmit={(event) => {
            event.preventDefault()
            if (dirty && trimmed) renameMut.mutate(trimmed)
          }}
        >
          {canRename ? (
            <Field label="Name" htmlFor="org-name">
              <TextInput id="org-name" value={name} onChange={setDraft} aria-required />
            </Field>
          ) : (
            <InfoRow label="Name" value={current.name} mono={false} />
          )}
          <Field
            label="Slug"
            htmlFor="org-slug"
            hint={`Part of every address in this organization (/o/${current.slug}/…), so it cannot be changed.`}
            last
          >
            <TextInput id="org-slug" value={current.slug} readOnly mono />
          </Field>
        </form>
      </SCard>

      <ProjectAccessCard org={current.slug} current={current.default_project_role} canEdit={canRename} />

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
 * on a project where they have no row. An owner or admin changes it; everyone
 * else reads it. Owners and admins see every project whatever it says, and a
 * project's own rows (Project › Access) override it, "No access" included.
 */
function ProjectAccessCard({
  org,
  current,
  canEdit,
}: {
  org: string
  current: DefaultProjectRole
  canEdit: boolean
}) {
  const qc = useQueryClient()
  const [draft, setDraft] = useState<DefaultProjectRole | null>(null)
  const value = draft ?? current
  const dirty = value !== current

  const saveMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (next: DefaultProjectRole) => orgsApi.update(org, { default_project_role: next }),
    onSuccess: (updated) => {
      qc.setQueryData(orgKey(org), updated)
      setDraft(null)
      // Which projects a member sees, and what they may do in them, follow it.
      void qc.invalidateQueries({ queryKey: orgRootKey(org) })
    },
  })

  const label = PROJECT_ROLE_OPTIONS.find((option) => option.value === value)?.label ?? value

  return (
    <SCard
      title="Default access to projects"
      description="What a member of this organization gets on a project they have not been added to. Owners and admins always see every project, and a project's Access settings can give someone more, less, or no access."
      footer={
        canEdit ? (
          <div className="flex w-full flex-wrap items-center justify-end gap-2">
            {saveMut.isError && (
              <p role="alert" className="m-0 mr-auto text-body-sm text-destructive">
                {getErrorMessage(saveMut.error)}
              </p>
            )}
            {saveMut.isSuccess && !dirty && (
              <p role="status" className="m-0 mr-auto text-body-sm text-success">Saved</p>
            )}
            <Button
              type="submit"
              form={PROJECT_ACCESS_FORM_ID}
              size="sm"
              aria-label="Save default access"
              disabled={!dirty || saveMut.isPending}
            >
              {saveMut.isPending ? 'Saving…' : 'Save'}
            </Button>
          </div>
        ) : undefined
      }
    >
      {canEdit ? (
        <form
          id={PROJECT_ACCESS_FORM_ID}
          noValidate
          onSubmit={(event) => {
            event.preventDefault()
            if (dirty && !saveMut.isPending) saveMut.mutate(value)
          }}
        >
          <Field
            label="Default access"
            htmlFor="org-default-project-role"
            hint={DEFAULT_ACCESS_HINTS[value]}
            last
          >
            <NativeSelect
              id="org-default-project-role"
              value={value}
              onChange={(next) => {
                saveMut.reset()
                setDraft(next as DefaultProjectRole)
              }}
              options={PROJECT_ROLE_OPTIONS}
              width="fill"
            />
          </Field>
        </form>
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
