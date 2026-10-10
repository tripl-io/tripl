import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  orgGroupKey,
  orgGroupsApi,
  orgGroupsKey,
  orgMembersKey,
  type OrgGroup,
  type OrgGroupDetail,
} from '@/api/orgGroups'
import { orgsApi } from '@/api/orgs'
import { useActiveOrg } from '@/components/active-org-context'
import { ErrorState } from '@/components/error-state'
import { Chip } from '@/components/primitives/chip'
import { Field, NativeSelect, SCard, SHeader, TextArea, TextInput } from '@/components/settings/kit'
import { ReadOnlyNotice, SectionSkeleton } from '@/components/states'
import { Button } from '@/components/ui/button'
import { useConfirm } from '@/hooks/useConfirm'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { useIsOwner } from '@/lib/permissions'
import { getErrorMessage } from '@/lib/utils'
import { countOf } from '@/lib/plural'

const CREATE_FORM_ID = 'create-group-form'
const EDIT_FORM_ID = 'edit-group-form'

/**
 * Organization › Groups (F20): named sets of the organization's members. In
 * Community a Docs note is shared with one (DocShareDialog); the Enterprise
 * edition also names them in escalation policies. The page says what they are
 * for, or an owner has no reason to make one. Every member sees them; owners
 * and admins create, rename and delete them and choose who is in each.
 */
export default function OrgGroupsSection() {
  const { slug } = useActiveOrg()
  return (
    <div>
      <SHeader
        title="Groups"
        description="Named sets of this organization's members, such as Analysts or On-call. Share a Docs note with a group instead of person by person. Only members of the organization can be in a group; someone who leaves the organization leaves its groups."
      />
      {slug ? (
        <OrgGroups key={slug} org={slug} />
      ) : (
        <ReadOnlyNotice className="mb-5">You are not a member of any organization.</ReadOnlyNotice>
      )}
    </div>
  )
}

function OrgGroups({ org }: { org: string }) {
  const qc = useQueryClient()
  const canManage = useIsOwner()
  const { confirm, dialog } = useConfirm()
  const [selected, setSelected] = useState<string | null>(null)
  const groupsQuery = useQuery({ queryKey: orgGroupsKey(org), queryFn: () => orgGroupsApi.list(org) })

  if (groupsQuery.isPending) return <SectionSkeleton variant="list" rows={3} label="Loading groups…" />
  if (groupsQuery.isError) {
    return (
      <ErrorState
        title="Couldn't load the organization's groups"
        error={groupsQuery.error}
        onRetry={() => {
          void groupsQuery.refetch()
        }}
      />
    )
  }
  const groups = groupsQuery.data
  const current = groups.find(g => g.id === selected) ?? null

  const handleDelete = async (group: OrgGroup) => {
    await confirm({
      title: `Delete ${group.name}?`,
      message:
        group.member_count > 0
          ? `The group and its ${countOf(group.member_count, 'membership', 'memberships')} are deleted. Its members stay in the organization.`
          : 'The group is deleted.',
      confirmLabel: 'Delete group',
      variant: 'danger',
      errorPrefix: 'Could not delete the group',
      pendingLabel: 'Deleting…',
      action: async () => {
        await orgGroupsApi.delete(org, group.id)
        if (selected === group.id) setSelected(null)
        await qc.invalidateQueries({ queryKey: orgGroupsKey(org) })
      },
    })
  }

  return (
    <>
      {dialog}
      {!canManage && (
        <ReadOnlyNotice className="mb-5">
          Owners and admins of the organization manage its groups.
        </ReadOnlyNotice>
      )}
      {canManage && <CreateGroupCard org={org} onCreated={setSelected} />}
      <SCard
        title="Groups"
        description={
          groups.length
            ? undefined
            : canManage
              ? 'No groups yet. Create one above, then choose it when you share a note in Docs.'
              : 'No groups yet.'
        }
      >
        {groups.map((group, index) => (
          <div
            key={group.id}
            className="flex items-center gap-3 px-4 py-[13px]"
            style={{ borderBottom: index === groups.length - 1 ? 'none' : '1px solid var(--border-subtle)' }}
          >
            <div className="min-w-0 flex-1">
              <div className="flex min-w-0 items-center gap-2">
                <span className="truncate text-body font-medium">{group.name}</span>
                {group.managed_by_scim && <ScimBadge />}
              </div>
              {group.description && (
                <div className="truncate text-body-sm text-fg-tertiary">{group.description}</div>
              )}
            </div>
            <span className="shrink-0 text-caption text-fg-tertiary">
              {countOf(group.member_count, 'member', 'members')}
            </span>
            <Button
              type="button"
              size="sm"
              variant="outline"
              aria-pressed={selected === group.id}
              aria-label={`${canManage ? 'Manage' : 'View'} ${group.name}`}
              onClick={() => setSelected(selected === group.id ? null : group.id)}
            >
              {canManage ? 'Manage' : 'View'}
            </Button>
            {/* A SCIM-managed group is deleted in the identity provider: the API answers 409. */}
            {canManage && !group.managed_by_scim && (
              <Button
                type="button"
                size="sm"
                variant="danger"
                aria-label={`Delete ${group.name}`}
                onClick={() => void handleDelete(group)}
              >
                Delete
              </Button>
            )}
          </div>
        ))}
      </SCard>
      {current && <GroupDetailCard key={current.id} org={org} groupId={current.id} canManage={canManage} />}
    </>
  )
}

function CreateGroupCard({ org, onCreated }: { org: string; onCreated: (id: string) => void }) {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const createMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () =>
      orgGroupsApi.create(org, { name: name.trim(), description: description.trim() }),
    onSuccess: created => {
      qc.setQueryData(orgGroupKey(org, created.id), created)
      void qc.invalidateQueries({ queryKey: orgGroupsKey(org), exact: true })
      setName('')
      setDescription('')
      onCreated(created.id)
    },
  })

  return (
    <SCard
      title="Create group"
      footer={
        <div className="flex w-full flex-wrap items-center justify-end gap-2">
          {createMut.isError && (
            <p role="alert" className="m-0 mr-auto text-body-sm text-destructive">
              {getErrorMessage(createMut.error)}
            </p>
          )}
          <Button type="submit" form={CREATE_FORM_ID} size="sm" disabled={!name.trim() || createMut.isPending}>
            {createMut.isPending ? 'Creating…' : 'Create group'}
          </Button>
        </div>
      }
    >
      <form
        id={CREATE_FORM_ID}
        noValidate
        onSubmit={event => {
          event.preventDefault()
          if (name.trim() && !createMut.isPending) createMut.mutate()
        }}
      >
        <Field label="Name" htmlFor="new-group-name">
          <TextInput id="new-group-name" value={name} onChange={setName} placeholder="e.g. Analysts" />
        </Field>
        <Field label="Description" htmlFor="new-group-description" last>
          <TextArea id="new-group-description" value={description} onChange={setDescription} rows={2} />
        </Field>
      </form>
    </SCard>
  )
}

function GroupDetailCard({ org, groupId, canManage }: { org: string; groupId: string; canManage: boolean }) {
  const detailQuery = useQuery({
    queryKey: orgGroupKey(org, groupId),
    queryFn: () => orgGroupsApi.get(org, groupId),
  })
  if (detailQuery.isPending) return <SectionSkeleton variant="form" label="Loading group…" />
  if (detailQuery.isError) {
    return (
      <ErrorState
        title="Couldn't load the group"
        error={detailQuery.error}
        onRetry={() => {
          void detailQuery.refetch()
        }}
      />
    )
  }
  const group = detailQuery.data
  // A group managed by the identity provider changes only through SCIM: the API
  // refuses a manual rename, delete or member edit (409), so the page offers
  // none of them. Its description stays editable here.
  const managed = group.managed_by_scim
  return (
    <>
      {canManage && managed && (
        <ReadOnlyNotice className="mb-5">
          {group.name} is managed by SCIM. Change its name and members, or delete it, in your identity
          provider. Its description stays editable here.
        </ReadOnlyNotice>
      )}
      {canManage && <EditGroupCard org={org} group={group} nameLocked={managed} />}
      <GroupMembersCard org={org} group={group} canManage={canManage && !managed} />
    </>
  )
}

/** Marks a group the identity provider created and keeps in sync over SCIM. */
function ScimBadge() {
  return (
    <Chip tone="info" className="shrink-0" title="Kept in sync by your identity provider; changes only through SCIM">
      Managed by SCIM
    </Chip>
  )
}

function EditGroupCard({
  org,
  group,
  nameLocked,
}: {
  org: string
  group: OrgGroupDetail
  /** A SCIM-managed group: its name is the identity provider's, only the description is editable. */
  nameLocked: boolean
}) {
  const qc = useQueryClient()
  const [name, setName] = useState(group.name)
  const [description, setDescription] = useState(group.description)
  const trimmed = name.trim()
  const dirty = trimmed !== group.name || description.trim() !== group.description
  const saveMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => {
      const data: { name?: string; description?: string } = {}
      if (trimmed !== group.name) data.name = trimmed
      if (description.trim() !== group.description) data.description = description.trim()
      return orgGroupsApi.update(org, group.id, data)
    },
    onSuccess: updated => {
      qc.setQueryData(orgGroupKey(org, group.id), updated)
      void qc.invalidateQueries({ queryKey: orgGroupsKey(org), exact: true })
      setName(updated.name)
      setDescription(updated.description)
    },
  })

  return (
    <SCard
      title={`Edit ${group.name}`}
      footer={
        <div className="flex w-full flex-wrap items-center justify-end gap-2">
          {saveMut.isError && (
            <p role="alert" className="m-0 mr-auto text-body-sm text-destructive">
              {getErrorMessage(saveMut.error)}
            </p>
          )}
          {saveMut.isSuccess && !dirty && (
            <p role="status" className="m-0 mr-auto text-body-sm text-success">Saved</p>
          )}
          <Button type="submit" form={EDIT_FORM_ID} size="sm" disabled={!dirty || !trimmed || saveMut.isPending}>
            {saveMut.isPending ? 'Saving…' : 'Save'}
          </Button>
        </div>
      }
    >
      <form
        id={EDIT_FORM_ID}
        noValidate
        onSubmit={event => {
          event.preventDefault()
          if (dirty && trimmed && !saveMut.isPending) saveMut.mutate()
        }}
      >
        <Field label="Name" htmlFor="edit-group-name">
          <TextInput
            id="edit-group-name"
            value={name}
            onChange={setName}
            aria-required
            disabled={nameLocked}
            readOnly={nameLocked}
          />
        </Field>
        <Field label="Description" htmlFor="edit-group-description" last>
          <TextArea id="edit-group-description" value={description} onChange={setDescription} rows={2} />
        </Field>
      </form>
    </SCard>
  )
}

function GroupMembersCard({
  org,
  group,
  canManage,
}: {
  org: string
  group: OrgGroupDetail
  canManage: boolean
}) {
  const qc = useQueryClient()
  const [pick, setPick] = useState('')
  const membersQuery = useQuery({
    queryKey: orgMembersKey(org),
    queryFn: () => orgsApi.members(org),
    enabled: canManage,
  })
  const refresh = async () => {
    await qc.invalidateQueries({ queryKey: orgGroupsKey(org) })
  }
  const addMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (userId: string) => orgGroupsApi.addMember(org, group.id, userId),
    onSuccess: async () => {
      setPick('')
      await refresh()
    },
  })
  const removeMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (userId: string) => orgGroupsApi.removeMember(org, group.id, userId),
    onSuccess: refresh,
  })

  const inGroup = new Set(group.members.map(m => m.user_id))
  const candidates = (membersQuery.data ?? []).filter(u => !inGroup.has(u.id))
  const error = addMut.error ?? removeMut.error

  return (
    <SCard
      title={`Members of ${group.name}`}
      description={group.members.length ? undefined : 'Nobody is in this group yet.'}
    >
      {group.members.map((member, index) => (
        <div
          key={member.user_id}
          className="flex items-center gap-3 px-4 py-[11px]"
          style={{
            borderBottom:
              index === group.members.length - 1 && !canManage ? 'none' : '1px solid var(--border-subtle)',
          }}
        >
          <div className="min-w-0 flex-1">
            <div className="truncate text-body font-medium">{member.name}</div>
            <div className="truncate text-body-sm text-fg-tertiary">{member.email}</div>
          </div>
          {canManage && (
            <Button
              type="button"
              size="sm"
              variant="danger"
              aria-label={`Remove ${member.name} from ${group.name}`}
              disabled={removeMut.isPending}
              onClick={() => removeMut.mutate(member.user_id)}
            >
              Remove
            </Button>
          )}
        </div>
      ))}
      {canManage && (
        <div className="flex flex-wrap items-center gap-2 px-4 py-[13px]">
          <NativeSelect
            aria-label="Organization member to add"
            value={pick}
            onChange={setPick}
            disabled={!candidates.length}
            options={[
              {
                value: '',
                label: membersQuery.isPending
                  ? 'Loading members…'
                  : candidates.length
                    ? 'Choose a member…'
                    : 'Every member is in this group',
              },
              ...candidates.map(u => ({ value: u.id, label: `${u.name} (${u.email})` })),
            ]}
          />
          <Button
            type="button"
            size="sm"
            disabled={!pick || addMut.isPending}
            onClick={() => addMut.mutate(pick)}
          >
            {addMut.isPending ? 'Adding…' : 'Add to group'}
          </Button>
          {error && (
            <p role="alert" className="m-0 w-full text-body-sm text-destructive">
              {getErrorMessage(error)}
            </p>
          )}
          {membersQuery.isError && (
            <p role="alert" className="m-0 w-full text-body-sm text-destructive">
              Couldn&rsquo;t load the organization&rsquo;s members: {getErrorMessage(membersQuery.error)}
            </p>
          )}
        </div>
      )}
    </SCard>
  )
}
