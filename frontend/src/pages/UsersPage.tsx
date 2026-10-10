import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'

import { invitationsApi, type Invitation, type InvitationCreated } from '@/api/invitations'
import { orgGroupsKey, orgMembersKey } from '@/api/orgGroups'
import { orgsApi, type OrgMemberRemoved } from '@/api/orgs'
import { usersApi } from '@/api/users'
import { AUTH_QUERY_KEY } from '@/components/auth-context'
import { useActiveOrg } from '@/components/active-org-context'
import { useAuth } from '@/components/auth-context'
import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { ReadOnlyNotice } from '@/components/states'
import { Button } from '@/components/ui/button'
import { FilterSearch } from '@/components/ui/filter-bar'
import { Skeleton } from '@/components/ui/skeleton'
import { UserAvatar } from '@/components/ui/user-avatar'
import { useConfirm } from '@/hooks/useConfirm'
import { Field, NativeSelect, SCard, TextInput } from '@/components/settings/kit'
import { RoleChip } from '@/components/settings/role-chip'
import { OneTimeSecretField } from '@/components/settings/one-time-secret'
import { ROLE_OPTIONS, type Role, type UserListItem } from '@/types'
import { formatDate } from '@/lib/datetime'
import { getErrorMessage } from '@/lib/utils'
import { isOwner as isOwnerRole } from '@/lib/permissions'
import { invitationsKey, usersKey } from '@/lib/queryKeys'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { SAVED_FEEDBACK_MS, useTransientFlag } from './settings-area/projectGeneralFields'
import { focusFirstInvalid } from '@/components/forms/validation'
import { usePublicDemo } from '@/lib/deploymentMode'
import { MemberResetLinkPanel } from './MemberResetLink'
import { countOf } from '@/lib/plural'

// The format rule said in words, where `type="email"` + `required` showed the
// browser's bubble instead.
const INVITE_EMAIL_MESSAGE = 'Enter an email address, like name@example.com.'
const EMAIL_SHAPE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

const INVITE_FORM_ID = 'invite-member-form'

/** Past this many members the roster gets a search box. */
const MEMBER_SEARCH_THRESHOLD = 10


/** What the Owner role hands over, said the same way wherever it is granted. */
const OWNER_POWERS =
  'Owners administer the whole organization: every project in it, its data sources and secrets, the audit log, every member’s role including other owners, and deleting any project.'

/**
 * Organization roles, lowest first (F20 PR4). An admin holds everything an
 * owner does except managing owners; a member holds only the projects they are
 * added to, at their project role.
 */
const ROLE_RANK: Readonly<Record<Role, number>> = { member: 0, admin: 1, owner: 2 }

/** The roles an actor may hand out: only an owner makes (or unmakes) an owner. */
function grantableRoles(actorIsOrgOwner: boolean): { value: Role; label: string }[] {
  return actorIsOrgOwner ? ROLE_OPTIONS : ROLE_OPTIONS.filter((option) => option.value !== 'owner')
}

function roleLabel(role: Role): string {
  return ROLE_OPTIONS.find((r) => r.value === role)?.label ?? role
}

/**
 * Invite one person without opening the instance to the world.
 *
 * The redeem link is shown exactly once, right after minting: the server never
 * returns it again, so this is the only chance to copy it. That is deliberate —
 * SMTP is optional here, so handing the link over out of band is a first-class
 * path rather than a fallback.
 *
 * Because it is shown once, the panel stays until it is dismissed, and minting
 * another invite over a link nobody copied asks first: it used to be replaced
 * without a word, and the first link was gone for good.
 */
export function InviteMemberCard({ actorIsOrgOwner }: { actorIsOrgOwner: boolean }) {
  const publicDemo = usePublicDemo()
  const qc = useQueryClient()
  const [email, setEmail] = useState('')
  const [emailError, setEmailError] = useState<string | null>(null)
  const [role, setRole] = useState<Role>('member')
  const [minted, setMinted] = useState<InvitationCreated | null>(null)
  const [everCopied, setEverCopied] = useState(false)
  const { confirm, dialog } = useConfirm()

  // `?invite=1` is where the command palette's "Invite member" lands: bring
  // the email field into view and focus it, then drop the param so a reload or
  // Back does not do it again. Same shape as the Branches tab's `?new=1`.
  const [searchParams, setSearchParams] = useSearchParams()
  const wantsInvite = searchParams.get('invite') === '1'
  useEffect(() => {
    if (!wantsInvite) return
    const input = document.getElementById('invite-email')
    input?.scrollIntoView?.({ block: 'center' })
    input?.focus({ preventScroll: true })
    const next = new URLSearchParams(searchParams)
    next.delete('invite')
    setSearchParams(next, { replace: true })
  }, [wantsInvite, searchParams, setSearchParams])

  const invitesQuery = useQuery({
    queryKey: invitationsKey(),
    queryFn: () => invitationsApi.list(),
  })
  const createMut = useMutation({
    // Rendered in the card (role="alert" below), so no toast as well.
    meta: SILENT_ERROR_META,
    mutationFn: () => invitationsApi.create(email.trim(), publicDemo ? 'member' : role),
    onSuccess: (created) => {
      setMinted(created)
      setEverCopied(false)
      setEmail('')
      qc.invalidateQueries({ queryKey: invitationsKey() })
    },
  })
  const revokeMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (id: string) => invitationsApi.revoke(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: invitationsKey() }),
  })

  const handleRevoke = async (inv: Invitation) => {
    const ok = await confirm({
      title: 'Revoke invitation',
      message:
        `Revoke the invitation for ${inv.email}? Their link stops working immediately, and `
        + 'it cannot be reissued — you would have to create a new invite and send the new link.',
      confirmLabel: 'Revoke',
      variant: 'danger',
    })
    if (ok) revokeMut.mutate(inv.id)
  }

  // Dismiss loses the show-once link as surely as replacing it does, so it
  // asks the same question when nobody copied it.
  const handleDismiss = async () => {
    if (!minted) return
    if (!everCopied) {
      const ok = await confirm({
        title: 'Discard the uncopied invite link?',
        message:
          `The link for ${minted.invitation.email} has not been copied, and it cannot be shown `
          + 'again. Revoke it and create a new one if you need it.',
        confirmLabel: 'Discard link',
        variant: 'danger',
      })
      if (!ok) return
    }
    setMinted(null)
  }

  const handleCreate = async () => {
    if (!email.trim() || createMut.isPending) return
    if (!EMAIL_SHAPE.test(email.trim())) {
      setEmailError(INVITE_EMAIL_MESSAGE)
      requestAnimationFrame(() => {
        const input = document.getElementById('invite-email')
        if (input?.parentElement) focusFirstInvalid(input.parentElement)
      })
      return
    }
    if (minted && !everCopied) {
      const ok = await confirm({
        title: 'Replace the uncopied invite link?',
        message:
          `The link for ${minted.invitation.email} has not been copied, and it cannot be shown `
          + 'again. A new invite replaces it on this page. It keeps working until it expires or '
          + 'is revoked, unless the new invite is for the same address, which invalidates it.',
        confirmLabel: 'Create new link',
        variant: 'primary',
      })
      if (!ok) return
    }
    if (!publicDemo && role === 'owner') {
      const ok = await confirm({
        title: 'Invite as Owner?',
        message:
          `${OWNER_POWERS} Whoever opens this link gets all of that, so send it only to `
          + `${email.trim()}.`,
        confirmLabel: 'Create owner invite',
        variant: 'danger',
      })
      if (!ok) return
    }
    createMut.mutate()
  }

  const invites = invitesQuery.data ?? []
  const acceptUrl = minted ? `${window.location.origin}${minted.accept_path}` : ''

  // Three cards with one job each, in the settings kit, where one hand-built
  // box explained the feature, held the form and listed the invites at 10-11px.
  return (
    <>
      {dialog}
      <SCard
        title="Invite a member"
        description={publicDemo
          ? 'Creates a single-use link for a colleague. No email is sent: copy the link and share it yourself. They sign in with Google at this address and receive viewer access to the demo projects of this organization, including ones generated later.'
          : 'Creates a single-use link for one address, at the role you pick. They see no project until they are added to one under Project settings › Access.'}
        footer={
          <div className="flex w-full flex-wrap items-center justify-end gap-2">
            {createMut.isError && (
              <p role="alert" className="m-0 mr-auto text-body-sm text-destructive">
                {getErrorMessage(createMut.error)}
              </p>
            )}
            {/* The page's main action, so the primary button, as "Create key"
                is on API keys. */}
            <Button
              type="submit"
              form={INVITE_FORM_ID}
              size="sm"
              disabled={createMut.isPending || !email.trim()}
            >
              {createMut.isPending ? 'Creating…' : 'Create invite link'}
            </Button>
          </div>
        }
      >
        {/* Kit rows and controls, not hand-styled elements: those were 32px
            and 24px tall, bordered differently from every other field, and had
            no focus ring at all for keyboard users. */}
        <form
          id={INVITE_FORM_ID}
          noValidate
          onSubmit={(e) => {
            e.preventDefault()
            void handleCreate()
          }}
        >
          <Field label="Email" htmlFor="invite-email" error={emailError}>
            <TextInput
              id="invite-email"
              type="email"
              aria-required
              autoComplete="off"
              value={email}
              onChange={(next) => {
                setEmail(next)
                setEmailError(null)
              }}
              placeholder="e.g. teammate@example.com"
            />
          </Field>
          {/* An owner invite forwarded to the wrong person is a full takeover,
              so the role says what it grants before the link exists. */}
          <Field
            label="Role"
            htmlFor="invite-role"
            hint={
              !publicDemo && role === 'owner' ? (
                <span id="invite-owner-warning" className="text-warning">
                  {OWNER_POWERS}
                </span>
              ) : undefined
            }
            last={!minted}
          >
            {/* The kit's Select, not a bare one. A native <select> keeps the
                platform's own widget: Chrome paints it with the UA's light
                background whatever `background` we hand it. */}
            <NativeSelect
              id="invite-role"
              value={publicDemo ? 'member' : role}
              onChange={(next) => setRole(next as Role)}
              options={publicDemo ? ROLE_OPTIONS.filter((option) => option.value === 'member') : grantableRoles(actorIsOrgOwner)}
              width="fill"
              aria-describedby={!publicDemo && role === 'owner' ? 'invite-owner-warning' : undefined}
            />
          </Field>
        </form>

        {minted && (
          <div className="space-y-1.5 px-4 py-[14px]">
            <div className="flex items-start justify-between gap-2">
              <p className="m-0 text-body-sm font-medium">
                {roleLabel(minted.invitation.role)} invite link for {minted.invitation.email} — copy
                it now
              </p>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="max-md:min-h-10"
                onClick={() => void handleDismiss()}
              >
                Dismiss
              </Button>
            </div>
            <p className="text-caption text-fg-tertiary">
              This link is shown once and cannot be retrieved later. It expires{' '}
              {formatDate(minted.expires_at)} and works a single time.
            </p>
            {/* Every copy counts, by hand too; otherwise every later invite
                warns about a link already copied. Keyed so a new link starts
                out uncopied. */}
            <OneTimeSecretField
              key={acceptUrl}
              value={acceptUrl}
              label="Invite link"
              noun="link"
              onCopied={() => setEverCopied(true)}
            />
          </div>
        )}
      </SCard>

      {invitesQuery.isError && (
        <ErrorState
          compact
          className="mb-5"
          title="Couldn't load pending invitations"
          error={invitesQuery.error}
          onRetry={() => {
            void invitesQuery.refetch()
          }}
        />
      )}

      {/* Rows styled like the API-key rows: the address, the role chip, when
          it expires, and a destructive Revoke. Shown when there are none too:
          the card used to vanish, so after the last revoke nothing said that
          no link was still out there. */}
      {invitesQuery.isSuccess && (
        <SCard
          title="Pending invitations"
          description={
            invites.length > 0
              ? `${invites.length} pending`
              : 'No pending invitations. A link you create is listed here until it is used or revoked.'
          }
        >
          {/* No body at all when empty: the description says it. */}
          {(invites.length > 0 || revokeMut.isError) && (
            <>
              {invites.map((inv: Invitation, index) => (
                <div
                  key={inv.id}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-[11px]"
                  style={{
                    borderBottom: index === invites.length - 1 ? 'none' : '1px solid var(--border-subtle)',
                  }}
                >
                  <span className="min-w-0 flex-1 truncate text-body-sm font-medium" title={inv.email}>
                    {inv.email}
                  </span>
                  <RoleChip role={inv.role} />
                  <span
                    className="w-[120px] shrink-0 text-right text-caption"
                    style={{ color: inv.is_expired ? 'var(--danger)' : 'var(--fg-subtle)' }}
                  >
                    {inv.is_expired ? 'Expired' : `Expires ${formatDate(inv.expires_at)}`}
                  </span>
                  {/* 28px, 40px on phones: a 24px Revoke sat beside other text.
                      */}
                  <Button
                    type="button"
                    size="sm"
                    variant="danger"
                    className="max-md:min-h-10"
                    onClick={() => {
                      void handleRevoke(inv)
                    }}
                    disabled={revokeMut.isPending && revokeMut.variables === inv.id}
                  >
                    {revokeMut.isPending && revokeMut.variables === inv.id ? 'Revoking…' : 'Revoke'}
                  </Button>
                </div>
              ))}
              {revokeMut.isError && (
                <p role="alert" className="m-0 px-4 py-3 text-body-sm text-destructive">
                  {getErrorMessage(revokeMut.error)}
                </p>
              )}
            </>
          )}
        </SCard>
      )}
    </>
  )
}

/** "2 project memberships and 1 API key went with it." — what a removal took. */
function removalSummary(who: string, removed: OrgMemberRemoved): string {
  const parts: string[] = []
  if (removed.project_memberships_removed > 0) {
    parts.push(countOf(removed.project_memberships_removed, 'project membership', 'project memberships'))
  }
  if (removed.api_keys_revoked > 0) parts.push(countOf(removed.api_keys_revoked, 'API key', 'API keys'))
  if (removed.invitations_revoked > 0) parts.push(countOf(removed.invitations_revoked, 'invitation', 'invitations'))
  if (removed.group_memberships_removed > 0) {
    parts.push(countOf(removed.group_memberships_removed, 'group membership', 'group memberships'))
  }
  return parts.length > 0 ? `Removed ${who}, with ${parts.join(', ')}.` : `Removed ${who}.`
}

/**
 * Organization › Members (F20 PR7): the active organization's roster, with the
 * role select, removal, ownership transfer and password reset links of
 * `/orgs/{org}/members`. With no organization known it reads `/users`, the
 * default organization's roster, as before organizations; the actions that
 * need the organization are not offered then.
 */
export default function UsersPage() {
  const qc = useQueryClient()
  const publicDemo = usePublicDemo()
  const { user: currentUser } = useAuth()
  const { slug: org } = useActiveOrg()
  // An org owner or admin manages members; only an owner manages owners.
  const isOwner = isOwnerRole(currentUser?.role)
  const actorIsOrgOwner = currentUser?.role === 'owner'
  const [removedNote, setRemovedNote] = useState<string | null>(null)

  const { confirm, dialog } = useConfirm()
  // A role change applies at once, with no Save step; it now says so on the
  // row, the way a settings page says "Saved".
  const [roleUpdated, markRoleUpdated, clearRoleUpdated] = useTransientFlag(SAVED_FEEDBACK_MS)

  const listQuery = useQuery({
    queryKey: usersKey(),
    queryFn: () => (org ? orgsApi.members(org) : usersApi.list()),
  })
  // The roster changed: refresh it, and the Groups page's copies of it (its
  // member picker and the groups a removed member has just left).
  const invalidateMembers = () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: usersKey() }),
      ...(org
        ? [
            qc.invalidateQueries({ queryKey: orgGroupsKey(org) }),
            qc.invalidateQueries({ queryKey: orgMembersKey(org) }),
          ]
        : []),
    ])
  const updateMut = useMutation({
    // The failure is shown on the row it belongs to, below.
    meta: SILENT_ERROR_META,
    mutationFn: ({ userId, role }: { userId: string; role: Role }) =>
      org ? orgsApi.updateMemberRole(org, userId, role) : usersApi.updateRole(userId, role),
    onMutate: clearRoleUpdated,
    onSuccess: () => {
      markRoleUpdated()
      return invalidateMembers()
    },
  })
  const removeMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (member: UserListItem) => orgsApi.removeMember(org ?? '', member.id),
    onMutate: () => setRemovedNote(null),
    onSuccess: (removed, member) => {
      setRemovedNote(removalSummary(member.name ?? member.email, removed))
      return invalidateMembers()
    },
  })
  const transferMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (member: UserListItem) => orgsApi.transferOwnership(org ?? '', member.id),
    onSuccess: () => {
      // The caller is an admin now: the session's role changes with it.
      void qc.invalidateQueries({ queryKey: AUTH_QUERY_KEY })
      return invalidateMembers()
    },
  })
  // The link it returns is the panel under the member's row; Dismiss resets
  // the mutation. One link on the page at a time.
  const resetLinkMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (member: UserListItem) => orgsApi.createPasswordResetLink(org ?? '', member.id),
  })

  const handleRemove = async (member: UserListItem) => {
    const who = member.name ?? member.email
    const ok = await confirm({
      title: `Remove ${who}?`,
      message:
        `${who} leaves the organization at once: their project memberships go, their API keys `
        + 'for it are revoked and their pending invitations are withdrawn. Invite them again to bring them back.',
      confirmLabel: 'Remove member',
      variant: 'danger',
    })
    if (!ok) return
    // One error line serves both actions: clear the other one's stale failure,
    // or it would keep speaking for this attempt.
    transferMut.reset()
    removeMut.mutate(member)
  }

  const handleTransfer = async (member: UserListItem) => {
    const who = member.name ?? member.email
    const ok = await confirm({
      title: `Transfer ownership to ${who}?`,
      message: `${OWNER_POWERS} ${who} becomes an owner, and you step down to Admin.`,
      confirmLabel: 'Transfer ownership',
      variant: 'danger',
    })
    if (!ok) return
    removeMut.reset()
    transferMut.mutate(member)
  }

  /**
   * Without email, "Forgot your password?" sends nothing: this is how a member
   * gets back in. Whoever opens the link owns the account, so it asks first.
   */
  const handleResetLink = async (member: UserListItem) => {
    const who = member.name ?? member.email
    const ok = await confirm({
      title: `Create a password reset link for ${who}?`,
      message:
        `Whoever opens the link can set a new password for ${member.email} and sign in as ${who}, `
        + `so give it to ${who} and no one else. It works once, for a limited time, and any `
        + 'earlier link for this account stops working.',
      confirmLabel: 'Create link',
      variant: 'primary',
    })
    if (ok) resetLinkMut.mutate(member)
  }

  const users = listQuery.data ?? []
  const [memberQuery, setMemberQuery] = useState('')
  const needle = memberQuery.trim().toLowerCase()
  const shownUsers = needle
    ? users.filter(
        (u) => (u.name ?? '').toLowerCase().includes(needle) || u.email.toLowerCase().includes(needle),
      )
    : users

  /**
   * Picking from the Select used to PATCH at once, so a stray arrow key or
   * wheel on a focused select could demote an owner or grant Owner, with no
   * undo. Granting Owner and every demotion now ask first; a
   * promotion short of Owner still applies directly.
   */
  const handleRoleChange = async (member: UserListItem, next: Role) => {
    if (next === member.role) return
    const who = member.name ?? member.email
    if (next === 'owner') {
      const ok = await confirm({
        title: `Make ${who} an owner?`,
        message: `${OWNER_POWERS} ${who} gets all of that as soon as you confirm.`,
        confirmLabel: 'Make owner',
        variant: 'danger',
      })
      if (!ok) return
    } else if (ROLE_RANK[next] < ROLE_RANK[member.role]) {
      const ok = await confirm({
        title: `Change ${who} to ${roleLabel(next)}?`,
        message:
          next === 'member'
            ? `${who} goes from ${roleLabel(member.role)} to Member: they keep only the projects they have been added to, at their project role, and lose organization administration.`
            : `${who} goes from ${roleLabel(member.role)} to ${roleLabel(next)} and can no longer manage owners.`,
        confirmLabel: `Change to ${roleLabel(next)}`,
        variant: 'danger',
      })
      if (!ok) return
    }
    updateMut.mutate({ userId: member.id, role: next })
  }

  return (
    <div>
      {dialog}
      {/* The section header above this (MembersSection) already says who is in
          the list. This used to restate it in a second vocabulary — "workspace"
          there, "instance" here — so two subtitles stacked directly on top of
          each other and a reader had to work out whether they named two
          different scopes. All that is left is the one fact the
          header does not carry, and only for the people it applies to. */}
      {/* The one read-only notice, not a loose paragraph larger than the
          section description (#237). */}
      {!isOwner && (
        <ReadOnlyNotice className="mb-5">Only owners and admins can change roles or invite people.</ReadOnlyNotice>
      )}

      {removedNote && (
        <p role="status" className="m-0 mb-3 text-body-sm text-success">{removedNote}</p>
      )}
      {removeMut.isError && (
        <p role="alert" className="m-0 mb-3 text-body-sm text-destructive">
          {getErrorMessage(removeMut.error)}
        </p>
      )}
      {transferMut.isError && (
        <p role="alert" className="m-0 mb-3 text-body-sm text-destructive">
          {getErrorMessage(transferMut.error)}
        </p>
      )}

      {/* A titled card with a count, like every other settings list, and
          named as they are ("All keys", "All scans") rather than repeating
          the page's own "Members" heading. */}
      <SCard
        title="All members"
        description={
          listQuery.isSuccess ? countOf(users.length, 'person', 'people') : undefined
        }
      >
        {users.length > MEMBER_SEARCH_THRESHOLD && (
          <div className="px-4 py-2.5 border-b border-b-border-subtle">
            <FilterSearch things="members" value={memberQuery} onValueChange={setMemberQuery} />
          </div>
        )}
        {listQuery.isLoading ? (
          <div aria-busy="true" aria-label="Loading users">
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
        ) : listQuery.isError ? (
          /* Before the error branch existed, a failed fetch fell through to
             "No users yet." — a page that always contains at least the reader,
             claiming to be empty, with nowhere to retry. */
          <div className="p-4">
            <ErrorState
              compact
              title="Couldn't load users"
              error={listQuery.error}
              onRetry={() => {
                void listQuery.refetch()
              }}
            />
          </div>
        ) : users.length === 0 ? (
          <EmptyState size="sm" headingLevel={3} title="No users yet." />
        ) : shownUsers.length === 0 ? (
          <EmptyState size="sm" headingLevel={3} title={`No member matches “${memberQuery.trim()}”`} />
        ) : (
          shownUsers.map((u: UserListItem) => (
            <div
              key={u.id}
              className="border-b px-4 py-2.5 last:border-0 border-border-subtle"
            >
              <div className="flex items-center gap-3">
                {/* One avatar colour, the same token the shell and the settings
                    sidebar use. The hue used to be hashed from the user id, so
                    the person reading this page saw their own initials in pink
                    here and in blue in the sidebar footer 30px away — one account
                    rendered as two. A hue carries no meaning worth
                    that. */}
                <UserAvatar name={u.name ?? u.email} size={28} />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-body-sm font-medium leading-tight">
                    {u.name ?? u.email}
                  </div>
                  <div
                    className="truncate text-caption leading-tight text-fg-tertiary"
                  >
                    {u.email}
                  </div>
                  {/* On phones the date is a second line, not gone. */}
                  <div className="text-caption leading-tight sm:hidden text-fg-tertiary">
                    Joined {formatDate(u.created_at)}
                  </div>
                </div>
                {/* The bare "2026-08-19" was a date with no question attached —
                    joined? invited? last seen? — in a table that has no column
                    headers to answer it. A date in the body font,
                    as a person reads it, not mono ISO. */}
                <span
                  className="hidden w-36 shrink-0 text-right text-caption sm:block text-fg-tertiary"
                >
                  Joined {formatDate(u.created_at)}
                </span>
                {/* The same box for the chip as for the select, so the column
                    does not alternate widths and heights row to row. Not on a
                    phone, where a fixed box beside a lone chip cut the email
                    short next to empty space. */}
                <div className="flex h-8 w-auto shrink-0 items-center justify-end sm:w-32">
                  {isOwner &&
                  u.id !== currentUser?.id &&
                  (actorIsOrgOwner || u.role !== 'owner') ? (
                    <NativeSelect
                      value={u.role}
                      aria-label={`Role for ${u.name ?? u.email}`}
                      onChange={(next) => {
                        void handleRoleChange(u, next as Role)
                      }}
                      disabled={updateMut.isPending}
                      options={grantableRoles(actorIsOrgOwner)}
                    />
                  ) : (
                    <RoleChip role={u.role} />
                  )}
                </div>
              </div>
              {org && isOwner && u.id !== currentUser?.id && (actorIsOrgOwner || u.role !== 'owner') && (
                <div className="mt-1.5 flex flex-wrap justify-end gap-2">
                  {!publicDemo && (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      onClick={() => void handleResetLink(u)}
                      disabled={resetLinkMut.isPending}
                      aria-label={`Create reset link for ${u.name ?? u.email}`}
                    >
                      {resetLinkMut.isPending && resetLinkMut.variables?.id === u.id
                        ? 'Creating…'
                        : 'Create reset link'}
                    </Button>
                  )}
                  {actorIsOrgOwner && u.role !== 'owner' && (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      onClick={() => void handleTransfer(u)}
                      disabled={transferMut.isPending}
                      aria-label={`Transfer ownership to ${u.name ?? u.email}`}
                    >
                      Transfer ownership
                    </Button>
                  )}
                  <Button
                    type="button"
                    size="sm"
                    variant="danger"
                    onClick={() => void handleRemove(u)}
                    disabled={removeMut.isPending && removeMut.variables?.id === u.id}
                    aria-label={`Remove ${u.name ?? u.email}`}
                  >
                    {removeMut.isPending && removeMut.variables?.id === u.id ? 'Removing…' : 'Remove'}
                  </Button>
                </div>
              )}
              {/* On the row it belongs to, naming the person: it used to sit
                  under the whole list, where it said nothing about whose role
                  had failed to change. The status region is always
                  mounted and only its text toggles: a live region inserted
                  already holding its text is often not announced. */}
              {(() => {
                const updated =
                  roleUpdated && updateMut.isSuccess && updateMut.variables?.userId === u.id
                return (
                  <p
                    role="status"
                    className={`m-0 text-right text-body-sm${updated ? ' mt-1.5' : ''} text-success`}
                  >
                    {updated ? 'Role updated' : ''}
                  </p>
                )
              })()}
              {updateMut.isError && updateMut.variables?.userId === u.id && (
                <p role="alert" className="m-0 mt-1.5 text-right text-body-sm text-destructive">
                  Could not change the role of {u.name ?? u.email}: {getErrorMessage(updateMut.error)}
                </p>
              )}
              {resetLinkMut.isError && resetLinkMut.variables?.id === u.id && (
                <p role="alert" className="m-0 mt-1.5 text-right text-body-sm text-destructive">
                  Could not create a reset link for {u.name ?? u.email}:{' '}
                  {getErrorMessage(resetLinkMut.error)}
                </p>
              )}
              {resetLinkMut.isSuccess && resetLinkMut.variables?.id === u.id && (
                <MemberResetLinkPanel
                  who={u.name ?? u.email}
                  link={resetLinkMut.data}
                  onDismiss={() => resetLinkMut.reset()}
                />
              )}
            </div>
          ))
        )}
      </SCard>
    </div>
  )
}
