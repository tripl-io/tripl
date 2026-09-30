import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { projectMembersApi } from '@/api/projectMembers'
import { usersApi } from '@/api/users'
import { useAuth } from '@/components/auth-context'
import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { Field, NativeSelect, SCard, SHeader } from '@/components/settings/kit'
import { RoleChip } from '@/components/settings/role-chip'
import { ReadOnlyNotice } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { UserAvatar } from '@/components/ui/user-avatar'
import { useConfirm } from '@/hooks/useConfirm'
import { formatDate } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { canManageProjectMembers, isOwner as isOwnerRole } from '@/lib/permissions'
import {
  projectKey,
  projectMembersKey,
  projectMembersQueryOptions,
  projectQueryOptions,
  usersKey,
} from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import { PROJECT_ROLE_OPTIONS, type ProjectMember, type ProjectMemberRole } from '@/types'

const ADD_FORM_ID = 'add-project-member-form'

/** The row roles an org owner/admin may hold: never `none` (they always have access). */
const ORG_ADMIN_ROLE_OPTIONS = PROJECT_ROLE_OPTIONS.filter((option) => option.value !== 'none')

/** The words a confirmation uses for a role, from the app-wide list. */
function roleWord(role: ProjectMemberRole): string {
  return PROJECT_ROLE_OPTIONS.find((option) => option.value === role)?.label ?? role
}

function memberName(member: Pick<ProjectMember, 'name' | 'email'>): string {
  return member.name || member.email
}

/**
 * Project · Access. Organization owners and admins see every project; anyone
 * else gets the organization's default access (Organization › Details), which
 * a row here overrides for this project: `editor`, `viewer`, or `none` ("No
 * access", which hides the project from them even when the default would
 * give it). This is where rows are added from the organization roster at a
 * role, re-roled, or removed (back to the default). Everyone who can open the
 * project can read the list; only managers see the controls.
 */
export default function ProjectMembersSection({ slug }: { slug: string }) {
  const qc = useQueryClient()
  const { user } = useAuth()
  const { confirm, dialog } = useConfirm()

  const projectQuery = useQuery(projectQueryOptions(slug))
  const membersQuery = useQuery(projectMembersQueryOptions(slug))
  const manager = canManageProjectMembers(user, projectQuery.data)

  // The roster is only needed to add someone, so readers never ask for it.
  const usersQuery = useQuery({
    queryKey: usersKey(),
    queryFn: () => usersApi.list(),
    enabled: manager,
  })

  const [pickedUserId, setPickedUserId] = useState('')
  const [pickedRole, setPickedRole] = useState<ProjectMemberRole>('editor')

  // The caller's own membership shapes `my_role` and can_mutate on the project,
  // so a change here refreshes the project as well as the list.
  const refresh = () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: projectMembersKey(slug) }),
      qc.invalidateQueries({ queryKey: projectKey(slug) }),
    ])

  // Every failure is shown on the card it belongs to, so no toast as well.
  const addMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: ({ userId, role }: { userId: string; role: ProjectMemberRole }) =>
      projectMembersApi.add(slug, userId, role),
    onSuccess: () => {
      setPickedUserId('')
      return refresh()
    },
  })
  const updateMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: ({ userId, role }: { userId: string; role: ProjectMemberRole }) =>
      projectMembersApi.updateRole(slug, userId, role),
    onSuccess: refresh,
  })
  const removeMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (userId: string) => projectMembersApi.remove(slug, userId),
    onSuccess: refresh,
  })

  const members = membersQuery.data ?? []
  const memberIds = new Set(members.map((member) => member.user_id))
  // Org owners/admins already see and manage every project of the organization,
  // so adding one would change nothing; they are left out of the picker.
  const candidates = (usersQuery.data ?? []).filter(
    (candidate) => !memberIds.has(candidate.id) && !isOwnerRole(candidate.role),
  )
  // Owners/admins can still hold a row (a creator's editor row, or a member
  // promoted after being added). They always have access, so the backend
  // refuses `none` for them (422): their row offers only Editor/Viewer, and a
  // leftover `none` row reads as "always has access" instead of "No access".
  const orgAdminIds = new Set(
    (usersQuery.data ?? []).filter((candidate) => isOwnerRole(candidate.role)).map((candidate) => candidate.id),
  )

  const resetErrors = () => {
    addMut.reset()
    updateMut.reset()
    removeMut.reset()
  }

  const handleAdd = () => {
    if (!pickedUserId || addMut.isPending) return
    resetErrors()
    addMut.mutate({ userId: pickedUserId, role: pickedRole })
  }

  const handleRoleChange = async (member: ProjectMember, next: ProjectMemberRole) => {
    if (next === member.role) return
    resetErrors()
    // A demotion takes away writing (or the whole project) at once, so it
    // asks; a promotion does not.
    const demotion = next === 'none' || (next === 'viewer' && member.role === 'editor')
    if (demotion) {
      const who = memberName(member)
      const word = roleWord(next)
      const ok = await confirm({
        title: `Change ${who} to ${word}?`,
        message:
          next === 'none'
            ? `${who} loses this project: it disappears from their project list, whatever the organization's default access.`
            : `${who} can still open this project but can no longer change anything in it.`,
        confirmLabel: `Change to ${word}`,
        variant: 'danger',
      })
      if (!ok) return
    }
    updateMut.mutate({ userId: member.user_id, role: next })
  }

  const handleRemove = async (member: ProjectMember) => {
    resetErrors()
    const who = memberName(member)
    const self = member.user_id === user?.id
    const ok = await confirm({
      title: 'Remove member',
      message: self
        ? "Remove yourself from this project? You are left with the organization's default access to it, which may be none, unless you are an owner or admin of the organization."
        : `Remove ${who} from this project? They are left with the organization's default access to it, which may be none.`,
      confirmLabel: 'Remove',
      variant: 'danger',
    })
    if (ok) removeMut.mutate(member.user_id)
  }

  const rowError = updateMut.isError
    ? `Could not change the role: ${getErrorMessage(updateMut.error)}`
    : removeMut.isError
      ? `Could not remove the member: ${getErrorMessage(removeMut.error)}`
      : null

  return (
    <div>
      {dialog}
      <SHeader
        title="Access"
        description="Who can see this project. Organization owners and admins see every project; everyone else gets the organization's default access (Organization › Details) unless a row here says otherwise. No access hides the project from that person."
      />

      {/* Wait for the project before claiming read-only: its creator is a manager. */}
      {projectQuery.isSuccess && !manager && (
        <ReadOnlyNotice className="mb-5">
          Only an organization owner or admin, or the person who created this project, can change who has access.
        </ReadOnlyNotice>
      )}

      {manager && (
        <SCard
          title="Add a member"
          description="Sets someone's access to this project, overriding the organization's default: Editor, Viewer, or No access to keep them out of it."
          footer={
            <div className="flex w-full flex-wrap items-center justify-end gap-2">
              {addMut.isError && (
                <p role="alert" className="m-0 mr-auto text-body-sm text-destructive">
                  {getErrorMessage(addMut.error)}
                </p>
              )}
              <Button
                type="submit"
                form={ADD_FORM_ID}
                size="sm"
                disabled={!pickedUserId || addMut.isPending}
              >
                {addMut.isPending ? 'Adding…' : 'Add member'}
              </Button>
            </div>
          }
        >
          {usersQuery.isError ? (
            <div className="p-4">
              <ErrorState
                compact
                title="Couldn't load the workspace members"
                error={usersQuery.error}
                onRetry={() => {
                  void usersQuery.refetch()
                }}
              />
            </div>
          ) : usersQuery.isSuccess && candidates.length === 0 ? (
            <p className="m-0 px-4 py-[14px] text-body-sm text-fg-tertiary">
              Everyone in the workspace already has a role here. Invite more people from Workspace ›
              Members, then add them here.
            </p>
          ) : (
            <form
              id={ADD_FORM_ID}
              noValidate
              onSubmit={(e) => {
                e.preventDefault()
                handleAdd()
              }}
            >
              <Field label="Person" htmlFor="project-member-user">
                <NativeSelect
                  id="project-member-user"
                  value={pickedUserId}
                  onChange={setPickedUserId}
                  disabled={!usersQuery.isSuccess}
                  width="fill"
                  options={[
                    {
                      value: '',
                      label: usersQuery.isSuccess ? 'Select a person…' : 'Loading people…',
                    },
                    ...candidates.map((candidate) => ({
                      value: candidate.id,
                      label: candidate.name
                        ? `${candidate.name} · ${candidate.email}`
                        : candidate.email,
                    })),
                  ]}
                />
              </Field>
              <Field
                label="Role"
                htmlFor="project-member-role"
                last
              >
                <NativeSelect
                  id="project-member-role"
                  value={pickedRole}
                  onChange={(next) => setPickedRole(next as ProjectMemberRole)}
                  options={PROJECT_ROLE_OPTIONS}
                  width="fill"
                />
              </Field>
            </form>
          )}
        </SCard>
      )}

      <SCard
        title="Members"
        description={
          membersQuery.isSuccess
            ? `${members.length} ${members.length === 1 ? 'person' : 'people'}`
            : undefined
        }
      >
        {membersQuery.isPending ? (
          <div aria-busy="true" aria-label="Loading members">
            {[0, 1, 2].map((index) => (
              <div
                key={index}
                className="flex items-center gap-3 border-b px-4 py-2.5 last:border-0 border-border-subtle"
              >
                <Skeleton className="h-7 w-7 shrink-0 rounded-full" />
                <div className="min-w-0 flex-1 space-y-1">
                  <Skeleton className="h-3 w-32" />
                  <Skeleton className="h-2.5 w-48" />
                </div>
                <Skeleton className="h-5 w-20 shrink-0" />
              </div>
            ))}
          </div>
        ) : membersQuery.isError ? (
          <div className="p-4">
            <ErrorState
              compact
              title="Couldn't load the members"
              error={membersQuery.error}
              onRetry={() => {
                void membersQuery.refetch()
              }}
            />
          </div>
        ) : members.length === 0 ? (
          <EmptyState
            size="sm"
            headingLevel={3}
            title="No members yet"
            description="Organization owners and admins see this project; everyone else gets the organization's default access until someone is added here."
          />
        ) : (
          members.map((member) => {
            const who = memberName(member)
            const orgAdmin = orgAdminIds.has(member.user_id)
            const busy =
              (updateMut.isPending && updateMut.variables?.userId === member.user_id)
              || (removeMut.isPending && removeMut.variables === member.user_id)
            return (
              <div
                key={member.user_id}
                className="flex flex-wrap items-center gap-3 border-b px-4 py-2.5 last:border-0 border-border-subtle"
              >
                <UserAvatar name={who} size={28} />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-body-sm font-medium leading-tight">{who}</div>
                  <div className="truncate text-caption leading-tight text-fg-tertiary">
                    {member.email}
                  </div>
                </div>
                <span className="hidden w-36 shrink-0 text-right text-caption sm:block text-fg-tertiary">
                  Added {formatDate(member.added_at)}
                </span>
                {manager ? (
                  <>
                    <div className="w-[120px] shrink-0">
                      {orgAdmin && member.role === 'none' ? (
                        <span className="block text-right text-caption text-fg-tertiary">
                          Owner/admin · always has access
                        </span>
                      ) : (
                        <NativeSelect
                          size="sm"
                          aria-label={`Role for ${who}`}
                          value={member.role}
                          disabled={busy}
                          onChange={(next) => {
                            void handleRoleChange(member, next as ProjectMemberRole)
                          }}
                          options={orgAdmin ? ORG_ADMIN_ROLE_OPTIONS : PROJECT_ROLE_OPTIONS}
                          width="fill"
                        />
                      )}
                    </div>
                    <Button
                      type="button"
                      size="sm"
                      variant="danger"
                      className="max-md:min-h-10"
                      aria-label={`Remove ${who}`}
                      disabled={busy}
                      onClick={() => {
                        void handleRemove(member)
                      }}
                    >
                      Remove
                    </Button>
                  </>
                ) : (
                  <span className="flex w-[120px] shrink-0 justify-end">
                    <RoleChip role={member.role} />
                  </span>
                )}
              </div>
            )
          })
        )}
        {rowError && (
          <p role="alert" className="m-0 px-4 py-3 text-body-sm text-destructive">
            {rowError}
          </p>
        )}
      </SCard>
    </div>
  )
}
