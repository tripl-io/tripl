import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'

import { orgsApi } from '@/api/orgs'
import { AUTH_QUERY_KEY } from '@/components/auth-context'
import { useActiveOrg } from '@/components/active-org-context'
import { ErrorState } from '@/components/error-state'
import { Field, InfoRow, SCard, SHeader, TextInput } from '@/components/settings/kit'
import { ReadOnlyNotice, SectionSkeleton } from '@/components/states'
import { Button } from '@/components/ui/button'
import { useConfirm } from '@/hooks/useConfirm'
import { orgHomePath } from '@/lib/activeOrg'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { useIsOrgOwner, useIsOwner, useIsPlatformAdmin } from '@/lib/permissions'
import { orgKey, orgsKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import { DangerRow } from './ProjectDangerRows'

/** The organization slug's shape: the backend's `ORG_SLUG_PATTERN`. */
const ORG_SLUG_SHAPE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/
const CREATE_FORM_ID = 'create-organization-form'
const RENAME_FORM_ID = 'rename-organization-form'

/**
 * Organization › Details, its General page (F20 PR7): the organization's name (an owner or admin
 * renames it), its slug (read-only: it is in every address, `/o/{slug}/…`, and
 * links already sent must keep working), the platform admin's "Create
 * organization", and the danger zone, where an owner deletes it. The default
 * organization cannot be deleted, so its danger zone is not drawn.
 */
export default function OrganizationGeneralSection() {
  const { slug } = useActiveOrg()
  const platformAdmin = useIsPlatformAdmin()

  return (
    <div>
      <SHeader
        title="Details"
        description="The organization this workspace belongs to. Its projects, data sources, API keys and members are its own."
      />
      {slug ? <OrganizationCards org={slug} /> : (
        <ReadOnlyNotice className="mb-5">You are not a member of any organization.</ReadOnlyNotice>
      )}
      {platformAdmin && <CreateOrganizationCard />}
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
    mutationFn: (next: string) => orgsApi.rename(org, next),
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
 * A platform admin's "Create organization". The creator becomes its owner and
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
      description="Platform admins only. You become its owner; invite its members from Invitations once you are in it."
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
