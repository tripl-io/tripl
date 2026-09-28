import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Globe2, Loader2, Lock, UserPlus, Users, X } from 'lucide-react'
import { orgGroupsApi, orgGroupsKey, orgMembersKey } from '@/api/orgGroups'
import { orgsApi } from '@/api/orgs'
import { ErrorState } from '@/components/error-state'
import { SectionSkeleton } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { IconButton } from '@/components/ui/icon-button'
import { Input } from '@/components/ui/input'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { getErrorMessage } from '@/lib/utils'
import type {
  DocPermission,
  DocShare,
  DocSharePrincipalType,
  DocSharing,
  DocVisibility,
} from '@/types/docs'
import { useDocSharing, useUpdateDocSharing, type DocSharingTarget } from './useDocs'

/** How many people and groups the "Add" list offers at once. */
const MAX_CANDIDATES = 8

const PERMISSION_OPTIONS = [
  { value: 'view', label: 'Can view' },
  { value: 'edit', label: 'Can edit' },
] as const satisfies ReadonlyArray<{ value: DocPermission; label: string }>

interface Candidate {
  principal_type: DocSharePrincipalType
  principal_id: string
  name: string
  detail: string
}

const shareKey = (share: Pick<DocShare, 'principal_type' | 'principal_id'>) =>
  `${share.principal_type}:${share.principal_id}`

/**
 * Who may read a note or a folder of notes (F24, GH #308): only its author,
 * specific people and organization groups, or everyone in the project (or
 * organization). A note follows the nearest folder setting until it gets its
 * own. The server decides every read — this dialog only edits the setting.
 */
export function DocShareDialog({
  slug,
  target,
  organizationSlug,
  organizationName,
  canManage,
  onClose,
}: {
  slug: string
  target: DocSharingTarget | null
  organizationSlug: string
  organizationName: string
  /**
   * The note's author or an organization owner or admin (for a folder: an
   * editor of the scope). The server has the last word; a refusal shows here.
   */
  canManage: boolean
  onClose: () => void
}) {
  return (
    <Dialog open={target !== null} onOpenChange={open => { if (!open) onClose() }}>
      {target && (
        <DocShareBody
          key={`${target.kind}:${target.scope}:${target.path}`}
          slug={slug}
          target={target}
          organizationSlug={organizationSlug}
          organizationName={organizationName}
          canManage={canManage}
          onClose={onClose}
        />
      )}
    </Dialog>
  )
}

function DocShareBody({
  slug,
  target,
  organizationSlug,
  organizationName,
  canManage,
  onClose,
}: {
  slug: string
  target: DocSharingTarget
  organizationSlug: string
  organizationName: string
  canManage: boolean
  onClose: () => void
}) {
  const sharing = useDocSharing(slug, target)
  const noun = target.kind === 'file' ? 'note' : 'folder'
  return (
    <DialogContent className="sm:max-w-lg">
      <DialogHeader>
        <DialogTitle>Share {noun}</DialogTitle>
        <DialogDescription>
          <span className="mono">{target.path}</span>
        </DialogDescription>
      </DialogHeader>
      {sharing.isPending ? (
        <DialogBody>
          <SectionSkeleton label="Loading sharing…" />
        </DialogBody>
      ) : sharing.isError ? (
        <DialogBody>
          <ErrorState compact title="Couldn't load the sharing settings" error={sharing.error} onRetry={() => void sharing.refetch()} />
        </DialogBody>
      ) : (
        <SharingForm
          slug={slug}
          target={target}
          initial={sharing.data}
          organizationSlug={organizationSlug}
          organizationName={organizationName}
          canManage={canManage}
          onClose={onClose}
        />
      )}
    </DialogContent>
  )
}

function SharingForm({
  slug,
  target,
  initial,
  organizationSlug,
  organizationName,
  canManage: canManageGuess,
  onClose,
}: {
  slug: string
  target: DocSharingTarget
  initial: DocSharing
  organizationSlug: string
  organizationName: string
  canManage: boolean
  onClose: () => void
}) {
  const [inherited, setInherited] = useState(initial.inherited)
  const [visibility, setVisibility] = useState<DocVisibility>(initial.visibility)
  const [shares, setShares] = useState<DocShare[]>(initial.shares)
  const [error, setError] = useState<string | null>(null)
  const update = useUpdateDocSharing(slug)
  const canManage = initial.can_manage ?? canManageGuess
  const locked = !canManage || inherited
  const everyone = target.scope === 'organization' ? `Everyone in ${organizationName}` : 'Everyone in this project'

  const onSave = async () => {
    setError(null)
    try {
      await update.mutateAsync({
        target,
        body: {
          visibility,
          inherited,
          shares:
            visibility === 'restricted'
              ? shares.map(({ principal_type, principal_id, permission }) => ({ principal_type, principal_id, permission }))
              : [],
        },
      })
      onClose()
    } catch (err) {
      setError(getErrorMessage(err))
    }
  }

  const options: { value: DocVisibility; label: string; hint: string; icon: typeof Lock }[] = [
    {
      value: 'private',
      // A folder has no single author: "private" there keeps each note to its
      // own author (not to whoever set the folder), so it is labelled that way.
      label: target.kind === 'file' ? 'Only me' : "Only each note's author",
      hint:
        target.kind === 'file'
          ? 'Only the author reads and edits it.'
          : 'Each note is read only by the person who wrote it — not by you, unless you wrote it.',
      icon: Lock,
    },
    {
      value: 'restricted',
      label: 'Specific people and groups',
      hint: 'The author, plus the people and groups below. They must also have access to the project.',
      icon: Users,
    },
    {
      value: 'level',
      label: everyone,
      hint: 'Every member reads it; editors edit it, as for any note.',
      icon: Globe2,
    },
  ]

  return (
    <>
      <DialogBody className="flex flex-col gap-4">
        <AccessSummary visibility={visibility} shares={shares} everyone={everyone} kind={target.kind} />

        <label className="flex items-start gap-2 text-body-sm">
          <Checkbox
            checked={inherited}
            disabled={!canManage}
            onCheckedChange={value => {
              const on = value === true
              setInherited(on)
              if (on) {
                // Back to what the folder says; the server resolves it again.
                setVisibility(initial.inherited ? initial.visibility : 'level')
                setShares(initial.inherited ? initial.shares : [])
              }
            }}
            aria-label="Follow the folder setting"
          />
          <span>
            Follow the folder setting
            <span className="block text-caption text-fg-tertiary">
              {initial.inherited_from
                ? <>Inherited from <span className="mono">{initial.inherited_from}</span>.</>
                : initial.inherited
                  ? 'No folder above sets one, so the default applies.'
                  : 'Use the setting of the nearest folder above that has one.'}
            </span>
          </span>
        </label>

        <fieldset className="m-0 flex flex-col gap-1.5 border-0 p-0" disabled={locked}>
          <legend className="mb-1.5 text-body-sm font-medium">Who can read it</legend>
          {options.map(option => {
            const Icon = option.icon
            return (
              <label
                key={option.value}
                className="flex cursor-pointer items-start gap-2 rounded-control border border-border px-3 py-2 has-[:checked]:border-[var(--accent)] has-[:disabled]:cursor-default"
              >
                <input
                  type="radio"
                  name="doc-visibility"
                  value={option.value}
                  checked={visibility === option.value}
                  onChange={() => setVisibility(option.value)}
                  className="mt-1"
                />
                <Icon className="mt-0.5 size-4 shrink-0 text-fg-tertiary" aria-hidden />
                <span className="min-w-0">
                  <span className="block text-body-sm font-medium">{option.label}</span>
                  <span className="block text-caption text-fg-tertiary">{option.hint}</span>
                </span>
              </label>
            )
          })}
        </fieldset>

        {visibility === 'restricted' && (
          <ShareList
            shares={shares}
            onChange={setShares}
            disabled={locked}
            organizationSlug={organizationSlug}
          />
        )}

        <p className="m-0 text-caption text-fg-tertiary">
          Organization owners and admins can still open a note that is not shared with them, by its link — every such
          read is recorded in the audit log. It never shows in their lists or searches.
        </p>
        {!canManage && (
          <p className="m-0 text-caption text-fg-tertiary">
            {target.kind === 'file'
              ? 'Only the author or an organization owner or admin changes who can read this note.'
              : 'Only an organization owner or admin, or a project editor when every note in the folder is theirs or open to everyone, changes who can read this folder.'}
          </p>
        )}
        {error && (
          <p role="alert" className="m-0 text-body-sm text-destructive">
            {error}
          </p>
        )}
      </DialogBody>
      <DialogFooter>
        <Button variant="outline" onClick={onClose}>
          {canManage ? 'Cancel' : 'Done'}
        </Button>
        {canManage && (
          <Button onClick={() => void onSave()} disabled={update.isPending}>
            {update.isPending && <Loader2 className="animate-spin" aria-hidden />}
            Save
          </Button>
        )}
      </DialogFooter>
    </>
  )
}

/** One sentence on who can read it now, in the reader's words. */
function AccessSummary({
  visibility,
  shares,
  everyone,
  kind,
}: {
  visibility: DocVisibility
  shares: DocShare[]
  everyone: string
  kind: DocSharingTarget['kind']
}) {
  const subject = kind === 'file' ? 'this note' : 'the notes in this folder'
  let text: string
  if (visibility === 'private') {
    text = kind === 'file' ? `Only the author can read ${subject}.` : `Only each note's own author can read ${subject}.`
  } else if (visibility === 'restricted') {
    const people = shares.filter(share => share.principal_type === 'user').length
    const groups = shares.length - people
    const parts = [
      people > 0 ? `${people} ${people === 1 ? 'person' : 'people'}` : null,
      groups > 0 ? `${groups} ${groups === 1 ? 'group' : 'groups'}` : null,
    ].filter(Boolean)
    text = parts.length > 0 ? `The author and ${parts.join(' and ')} can read ${subject}.` : `Only the author can read ${subject} until you add someone.`
  } else {
    text = `${everyone} can read ${subject}.`
  }
  return (
    <p role="status" className="m-0 rounded-control bg-bg-sunken px-3 py-2 text-body-sm">
      {text}
    </p>
  )
}

function ShareList({
  shares,
  onChange,
  disabled,
  organizationSlug,
}: {
  shares: DocShare[]
  onChange: (next: DocShare[]) => void
  disabled: boolean
  organizationSlug: string
}) {
  const [filter, setFilter] = useState('')
  const members = useQuery({
    queryKey: orgMembersKey(organizationSlug),
    queryFn: () => orgsApi.members(organizationSlug),
    enabled: !disabled && Boolean(organizationSlug),
  })
  const groups = useQuery({
    queryKey: orgGroupsKey(organizationSlug),
    queryFn: () => orgGroupsApi.list(organizationSlug),
    enabled: !disabled && Boolean(organizationSlug),
  })

  const taken = useMemo(() => new Set(shares.map(shareKey)), [shares])
  const candidates = useMemo<Candidate[]>(() => {
    const all: Candidate[] = [
      ...(groups.data ?? []).map(group => ({
        principal_type: 'group' as const,
        principal_id: group.id,
        name: group.name,
        detail: 'Group',
      })),
      ...(members.data ?? []).map(member => ({
        principal_type: 'user' as const,
        principal_id: member.id,
        name: member.name || member.email,
        detail: member.email,
      })),
    ]
    const needle = filter.trim().toLowerCase()
    return all
      .filter(candidate => !taken.has(shareKey(candidate)))
      .filter(candidate => needle === '' || `${candidate.name} ${candidate.detail}`.toLowerCase().includes(needle))
      .slice(0, MAX_CANDIDATES)
  }, [groups.data, members.data, filter, taken])

  const setPermission = (key: string, permission: DocPermission) =>
    onChange(shares.map(share => (shareKey(share) === key ? { ...share, permission } : share)))
  const remove = (key: string) => onChange(shares.filter(share => shareKey(share) !== key))
  const add = (candidate: Candidate) => {
    onChange([
      ...shares,
      {
        principal_type: candidate.principal_type,
        principal_id: candidate.principal_id,
        name: candidate.name,
        permission: 'view',
      },
    ])
    setFilter('')
  }

  return (
    <section aria-labelledby="doc-share-list" className="flex flex-col gap-2">
      <h3 id="doc-share-list" className="m-0 text-body-sm font-medium">
        Shared with
      </h3>
      {shares.length === 0 ? (
        <p className="m-0 text-caption text-fg-tertiary">Nobody yet.</p>
      ) : (
        <ul className="m-0 flex list-none flex-col divide-y divide-border-subtle rounded-control border border-border p-0">
          {shares.map(share => {
            const key = shareKey(share)
            return (
              <li key={key} className="flex items-center gap-2 px-3 py-1.5">
                {share.principal_type === 'group' ? (
                  <Users className="size-3.5 shrink-0 text-fg-tertiary" aria-label="Group" />
                ) : (
                  <UserPlus className="size-3.5 shrink-0 text-fg-tertiary" aria-label="Person" />
                )}
                <span className="min-w-0 flex-1 truncate text-body-sm">{share.name}</span>
                <SegmentedControl<DocPermission>
                  aria-label={`Permission for ${share.name}`}
                  size="sm"
                  value={share.permission}
                  onChange={value => { if (!disabled) setPermission(key, value) }}
                  options={PERMISSION_OPTIONS.map(option => ({ ...option, disabled }))}
                />
                <IconButton
                  label={`Remove ${share.name}`}
                  size="icon-xs"
                  variant="ghost"
                  disabled={disabled}
                  onClick={() => remove(key)}
                >
                  <X />
                </IconButton>
              </li>
            )
          })}
        </ul>
      )}
      {!disabled && (
        <div className="flex flex-col gap-1.5">
          <Input
            type="search"
            value={filter}
            onChange={e => setFilter(e.target.value)}
            placeholder="Add people or groups"
            aria-label="Add people or groups"
            className="h-8"
          />
          {(members.isError || groups.isError) && (
            <p role="alert" className="m-0 text-caption text-destructive">
              Couldn't load the organization's members and groups: {getErrorMessage(members.error ?? groups.error)}
            </p>
          )}
          {candidates.length > 0 && (
            <ul aria-label="Suggestions" className="m-0 flex list-none flex-col p-0">
              {candidates.map(candidate => (
                <li key={shareKey(candidate)}>
                  <button
                    type="button"
                    onClick={() => add(candidate)}
                    aria-label={`Add ${candidate.name}`}
                    className="flex w-full min-w-0 items-center gap-2 rounded-control px-2 py-1 text-left text-body-sm hover:bg-surface-hover"
                  >
                    {candidate.principal_type === 'group' ? (
                      <Users className="size-3.5 shrink-0 text-fg-tertiary" aria-hidden />
                    ) : (
                      <UserPlus className="size-3.5 shrink-0 text-fg-tertiary" aria-hidden />
                    )}
                    <span className="truncate">{candidate.name}</span>
                    <span className="truncate text-caption text-fg-tertiary">{candidate.detail}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}
