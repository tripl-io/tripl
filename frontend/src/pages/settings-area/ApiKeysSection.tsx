import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, KeyRound, Lock, Plus } from 'lucide-react'
import { apiKeysApi } from '@/api/apiKeys'
import { apiKeysKey, projectsQueryOptions } from '@/lib/queryKeys'
import { useAuth } from '@/components/auth-context'
import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { Chip } from '@/components/primitives/chip'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { INPUT_BASE, INPUT_CLASS } from '@/components/settings/input-style'
import { REQUIRED_MESSAGE, focusFirstInvalid, invalidAria } from '@/components/forms/validation'
import { useConfirm } from '@/hooks/useConfirm'
import { useCopyToClipboard } from '@/hooks/useCopyToClipboard'
import { formatIsoDate } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { getErrorMessage } from '@/lib/utils'
import { Field, SCard, SHeader, NativeSelect, TextInput } from '@/components/settings/kit'
import { describeKeyCounts, describeRevealedKey, isKeyInactive } from './apiKeyStatus'
import type { ApiKey, ApiKeyScope, ApiKeyWithToken } from '@/types'
import { canWrite } from '@/lib/permissions'

const MAX_EXPIRY_DAYS = 3650
const NEW_KEY_FORM_ID = 'new-api-key-form'

/** Inline error for the optional expiry, or null when it is blank or valid. */
function expiryError(raw: string): string | null {
  if (raw.trim() === '') return null
  const days = Number(raw)
  if (!Number.isInteger(days) || days < 1 || days > MAX_EXPIRY_DAYS) {
    return `Enter a whole number of days from 1 to ${MAX_EXPIRY_DAYS}, or leave it blank.`
  }
  return null
}

/**
 * Workspace · API keys. Reuses the real apiKeysApi wiring (the same create /
 * reveal-once / revoke flow as AccountPage) rendered in the takeover idiom: a
 * "shown once" warning banner plus a card of active keys with scope chips.
 */
export default function ApiKeysSection() {
  const qc = useQueryClient()
  const { user } = useAuth()
  const { confirm, dialog } = useConfirm()

  const [showForm, setShowForm] = useState(false)
  const [name, setName] = useState('')
  // "Required" under the name after an empty Create, not the browser's
  // bubble.
  const [nameError, setNameError] = useState<string | null>(null)
  const [scope, setScope] = useState<ApiKeyScope>('read')
  const [projectSlug, setProjectSlug] = useState('')
  const [expiresInDays, setExpiresInDays] = useState('')
  const [revealed, setRevealed] = useState<ApiKeyWithToken | null>(null)
  // Revoked keys stay listed forever; they fold away behind a count.
  const [showRevoked, setShowRevoked] = useState(false)
  const tokenRef = useRef<HTMLInputElement>(null)
  const { state: copyState, copy, reset: resetCopy } = useCopyToClipboard(tokenRef)

  const listQuery = useQuery({
    queryKey: apiKeysKey(),
    queryFn: () => apiKeysApi.list(),
    meta: SILENT_ERROR_META,
  })
  const projectsQuery = useQuery(projectsQueryOptions())

  const projectNameById = (projectsQuery.data ?? []).reduce<Record<string, string>>((acc, p) => {
    acc[p.id] = p.name
    return acc
  }, {})

  const resetDraft = () => {
    setName('')
    setScope('read')
    setProjectSlug('')
    setExpiresInDays('')
  }

  const createMut = useMutation({
    mutationFn: () =>
      apiKeysApi.create({
        name: name.trim(),
        scope,
        expires_in_days: expiresInDays ? Number(expiresInDays) : null,
        project_slug: projectSlug || null,
      }),
    meta: SILENT_ERROR_META,
    onSuccess: (created) => {
      qc.invalidateQueries({ queryKey: apiKeysKey() })
      setShowForm(false)
      resetCopy()
      setRevealed(created)
      resetDraft()
    },
  })

  // Cancel used to only hide the card, so the next "Create key" reopened it
  // with the abandoned name, scope and the previous failure still showing.
  const cancelForm = () => {
    setShowForm(false)
    resetDraft()
    createMut.reset()
  }

  // Per key, not off the mutation: rows other than the one being revoked stay
  // live, so a second revoke can start while the first is in flight, and
  // `revokeMut.variables`/`error` then describe only the latest one. A failure
  // on the first would vanish — on a credentials surface, a leaked key that
  // looks revoked.
  const [pendingRevokes, setPendingRevokes] = useState<ReadonlySet<string>>(() => new Set())
  const [revokeFailures, setRevokeFailures] = useState<ReadonlyMap<string, string>>(
    () => new Map(),
  )

  const revokeMut = useMutation({
    mutationFn: (keyId: string) => apiKeysApi.revoke(keyId),
    // Rendered below the list: a failed revoke on a credentials surface must
    // not read as a revoke that worked.
    meta: SILENT_ERROR_META,
    // Mutation-level callbacks run for every call; the ones passed to
    // mutate() only for the latest.
    onMutate: (keyId) => {
      setPendingRevokes((current) => new Set(current).add(keyId))
      setRevokeFailures((current) => {
        const next = new Map(current)
        next.delete(keyId)
        return next
      })
    },
    onError: (error, keyId) => {
      setRevokeFailures((current) => new Map(current).set(keyId, getErrorMessage(error)))
    },
    onSettled: (_data, _error, keyId) => {
      setPendingRevokes((current) => {
        const next = new Set(current)
        next.delete(keyId)
        return next
      })
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: apiKeysKey() }),
  })

  const handleRevoke = async (key: ApiKey) => {
    const ok = await confirm({
      title: 'Revoke API key',
      message: `Revoke "${key.name}"? Any agent or script using this key will start receiving 401s immediately.`,
      confirmLabel: 'Revoke',
      variant: 'danger',
    })
    if (ok) revokeMut.mutate(key.id)
  }

  const keys = listQuery.data ?? []
  const canCreateWriteKeys = canWrite(user?.role)
  const expiryProblem = expiryError(expiresInDays)

  // The card used to headline "Active keys · N keys" off the unfiltered list,
  // so revoked and expired tokens were counted as live ones on a credentials
  // surface. Count only the keys that can still authenticate,
  // and name the inactive remainder explicitly.
  const inactiveCount = keys.filter((k) => isKeyInactive(k)).length
  const activeCount = keys.length - inactiveCount

  // What each scope lets a client do, in the reader's words rather than HTTP
  // verbs.
  const scopeOptions = [
    { value: 'read', label: 'Read-only: can view the plan and data' },
    ...(canCreateWriteKeys ? [{ value: 'write', label: 'Read & write: same access as an editor' }] : []),
  ]
  const revokedCount = keys.filter((k) => k.revoked_at != null).length
  const shownKeys = showRevoked ? keys : keys.filter((k) => k.revoked_at == null)
  const openForm = () => {
    if (!canCreateWriteKeys) setScope('read')
    setShowForm(true)
  }
  const projectOptions = [
    { value: '', label: 'All projects — full account reach' },
    ...(projectsQuery.data ?? []).map((p) => ({ value: p.slug, label: p.name })),
  ]

  return (
    <div>
      {dialog}
      <SHeader
        title="API keys"
        description="Long-lived bearer tokens for non-browser clients (LLM agents, CLI scripts)."
        actions={
          // Off while the form is open: pressing it again did nothing visible.
          // Marked for the `c` shortcut, which otherwise looks for "New …".
          <Button size="sm" onClick={openForm} disabled={showForm} data-create-action="">
            <Plus className="h-3.5 w-3.5" />
            Create key
          </Button>
        }
      />

      {/* Create key — inline page-style form (no modal). Kit rows and the kit
          Select, not bare <select>s: a native select keeps the platform's light
          widget in dark mode. */}
      {showForm && (
        <SCard
          title="New API key"
          description="Generate a long-lived bearer token for non-browser clients."
          // The card's own action band, the sunken footer every settings card
          // uses, with the page's `sm` buttons.
          footer={
            <div className="flex w-full flex-wrap items-center justify-end gap-2">
              {createMut.isError && (
                <p role="alert" className="m-0 mr-auto text-body-sm text-destructive">
                  {getErrorMessage(createMut.error)}
                </p>
              )}
              <Button type="button" variant="outline" size="sm" onClick={cancelForm}>
                Cancel
              </Button>
              <Button
                type="submit"
                form={NEW_KEY_FORM_ID}
                size="sm"
                // An empty name is said inline on press, not by a
                // silently disabled button.
                disabled={createMut.isPending || expiryProblem != null}
              >
                {createMut.isPending ? 'Generating…' : 'Generate'}
              </Button>
            </div>
          }
        >
          <form
            id={NEW_KEY_FORM_ID}
            noValidate
            onSubmit={(e) => {
              e.preventDefault()
              const missing = name.trim() ? null : REQUIRED_MESSAGE
              setNameError(missing)
              if (missing || expiryProblem) {
                const form = e.currentTarget
                requestAnimationFrame(() => focusFirstInvalid(form))
                return
              }
              createMut.mutate()
            }}
          >
            <Field label="Name" htmlFor="key-name" required error={nameError}>
              <input
                id="key-name"
                value={name}
                onChange={(e) => {
                  setName(e.target.value)
                  if (e.target.value.trim()) setNameError(null)
                }}
                placeholder="e.g. claude-agent"
                // A raw <input> does not read the Field's slot, so it carries
                // the row's state itself: announced as required, and marked
                // invalid so the message is read with it and
                // focusFirstInvalid can land on it.
                aria-required
                {...invalidAria('key-name', nameError)}
                // eslint-disable-next-line jsx-a11y/no-autofocus -- form revealed by explicit "Create key" click; focusing its first input is expected
                autoFocus
                style={INPUT_BASE}
                className={INPUT_CLASS}
              />
            </Field>
            {scopeOptions.length > 1 ? (
              <Field label="Scope" htmlFor="key-scope">
                <NativeSelect
                  id="key-scope"
                  value={scope}
                  onChange={(value) => setScope(value as ApiKeyScope)}
                  options={scopeOptions}
                  width="fill"
                />
              </Field>
            ) : (
              // One choice is a fact, not a select.
              <Field label="Scope" htmlFor={false}>
                <span className="block pt-1.5 text-body-sm">{scopeOptions[0]!.label}</span>
              </Field>
            )}
            <Field label="Project (optional)" htmlFor="key-project">
              <NativeSelect
                id="key-project"
                value={projectSlug}
                onChange={setProjectSlug}
                options={projectOptions}
                width="fill"
              />
            </Field>
            <Field
              label="Expires in (optional)"
              htmlFor="key-expires"
              hint={
                expiryProblem ? (
                  <span id="key-expires-error" className="text-danger">
                    {expiryProblem}
                  </span>
                ) : (
                  'Leave blank for a key that never expires.'
                )
              }
              last
            >
              <TextInput
                id="key-expires"
                type="number"
                value={expiresInDays}
                onChange={setExpiresInDays}
                suffix="days"
                aria-invalid={expiryProblem != null}
                aria-describedby={expiryProblem ? 'key-expires-error' : undefined}
              />
            </Field>
          </form>
        </SCard>
      )}

      {/* About keys that exist: above an empty list it warned about nothing.
          The create form shows it too, where the key is born. */}
      {(showForm || keys.length > 0) && (
      <div
        className="mb-5 flex gap-2.5 rounded-card px-3.5 py-3"
        style={{
          background: 'var(--warning-soft)',
          border: '1px solid color-mix(in oklab, var(--warning) 35%, var(--border))',
        }}
      >
        <Lock className="mt-px size-4 shrink-0 text-warning" />
        <div className="text-body-sm leading-[1.5] text-fg-secondary">
          Keys are shown in full only once at creation. Treat them like passwords — revoke
          immediately if exposed.
        </div>
      </div>
      )}

      <SCard
        title="All keys"
        // No count until there is a list to count: "0 active" above a failed
        // load reads as "you have no credentials".
        description={listQuery.isSuccess ? describeKeyCounts(activeCount, inactiveCount) : undefined}
      >
        {listQuery.isPending ? (
          <div aria-busy="true" aria-label="Loading API keys" className="space-y-3 px-4 py-3">
            {[0, 1].map((index) => (
              <div key={index} className="flex items-center gap-3">
                <Skeleton className="h-[30px] w-[30px] shrink-0 rounded-lg" />
                <div className="min-w-0 flex-1 space-y-1">
                  <Skeleton className="h-3 w-32" />
                  <Skeleton className="h-2.5 w-20" />
                </div>
              </div>
            ))}
          </div>
        ) : listQuery.isError ? (
          // A failed load used to fall through to "No API keys yet", so live
          // keys looked nonexistent and invited minting duplicates.
          <div className="px-4 py-3">
            <ErrorState
              compact
              title="Couldn't load API keys"
              error={listQuery.error}
              onRetry={() => {
                void listQuery.refetch()
              }}
            />
          </div>
        ) : shownKeys.length === 0 ? (
          // What a key is for and the next step, not one grey line.
          // Also when every key is revoked and hidden: the card held nothing
          // but the "Show revoked" toggle.
          <EmptyState
            size="sm"
            headingLevel={3}
            icon={KeyRound}
            title={keys.length === 0 ? 'No API keys yet' : 'No active keys'}
            description={
              keys.length === 0
                ? 'Create a key to let an agent or script read your tracking plan.'
                : 'Every key here has been revoked. Create a new one to let an agent or script read your tracking plan.'
            }
            action={
              showForm ? undefined : (
                <Button size="sm" onClick={openForm}>
                  <Plus className="h-3.5 w-3.5" />
                  {keys.length === 0 ? 'Create your first key' : 'Create a new key'}
                </Button>
              )
            }
          />
        ) : (
          shownKeys.map((k, i) => {
            // One rule for the row and the heading count: isKeyInactive is
            // inclusive at the expiry instant, like the backend.
            const revoked = k.revoked_at != null
            const expired = !revoked && isKeyInactive(k)
            const revoking = pendingRevokes.has(k.id)
            return (
              // The row reads its own width, not the viewport's: from
              // `md` the settings rail is pinned and the column is ~424px at
              // 768px, so a viewport breakpoint put the ~560px line in too
              // narrow a card. Same 560px container step as FormRow.
              <div key={k.id} className="@container">
                <div
                  // A grid when narrow — icon, name and Revoke on the first line,
                  // scope, project and status below — and one flex line from a
                  // 560px row up. The fixed-width single line measures ~560px.
                  className="grid grid-cols-[30px_minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1.5 px-4 py-[13px] @min-[560px]:flex"
                  style={{
                    borderBottom:
                      i === shownKeys.length - 1 && revokedCount === 0 ? 'none' : '1px solid var(--border-subtle)',
                    opacity: revoked || expired ? 0.6 : 1,
                  }}
                >
                  <div
                    className="col-start-1 row-start-1 flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-lg bg-bg-sunken border border-border-subtle text-fg-secondary"
                  >
                    <Lock className="h-3.5 w-3.5" />
                  </div>
                  {/* The name takes the free width: a fixed 180px cut "created …"
                      even at 1440 beside an empty middle column. */}
                  <div className="col-start-2 row-start-1 min-w-0 @min-[560px]:flex-1">
                    <div className="truncate text-body font-medium" title={k.name}>
                      {k.name}
                    </div>
                    <div className="mono mt-px truncate text-caption text-fg-tertiary">
                      {k.key_prefix}…
                    </div>
                  </div>
                  <div className="col-span-2 col-start-2 row-start-2 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 @min-[560px]:contents">
                    <Chip
                      tone={k.scope === 'write' ? 'warning' : 'success'}
                      size="sm"
                      className="justify-center @min-[560px]:w-[72px] @min-[560px]:shrink-0"
                    >
                      {k.scope}
                    </Chip>
                    <div
                      className="min-w-0 truncate text-caption @min-[560px]:w-[120px] @min-[560px]:shrink-0 text-fg-tertiary"
                    >
                      {k.project_id
                        ? (projectNameById[k.project_id] ?? k.project_id)
                        : 'All projects'}
                    </div>
                    <div
                      className="text-caption @min-[560px]:w-[130px] @min-[560px]:shrink-0 @min-[560px]:text-right text-fg-tertiary"
                    >
                      <div>created {formatIsoDate(k.created_at)}</div>
                      <div>
                        {revoked
                          ? `revoked ${formatIsoDate(k.revoked_at!)}`
                          : expired
                            ? 'expired'
                            : k.last_used_at
                              ? `used ${formatIsoDate(k.last_used_at)}`
                              : 'never used'}
                      </div>
                      {!revoked && !expired && (
                        <div>
                          {k.expires_at ? `expires ${formatIsoDate(k.expires_at)}` : 'no expiry'}
                        </div>
                      )}
                    </div>
                  </div>
                  {!revoked && (
                    // Bare red: a destructive row action, not a neutral one.
                    <Button
                      variant="danger"
                      size="sm"
                      className="col-start-3 row-start-1"
                      onClick={() => {
                        void handleRevoke(k)
                      }}
                      disabled={revoking}
                      // The visible label is the same on every row; the name
                      // says which key a screen-reader user is about to revoke.
                      aria-label={`${revoking ? 'Revoking…' : 'Revoke'} ${k.name}`}
                    >
                      {revoking ? 'Revoking…' : 'Revoke'}
                    </Button>
                  )}
                </div>
              </div>
            )
          })
        )}
        {listQuery.isSuccess && revokedCount > 0 && (
          <div className="px-4 py-2">
            <Button
              variant="ghost"
              size="sm"
              aria-expanded={showRevoked}
              onClick={() => setShowRevoked((open) => !open)}
            >
              {showRevoked ? 'Hide revoked' : `Show revoked (${revokedCount})`}
            </Button>
          </div>
        )}
        {[...revokeFailures].map(([keyId, message]) => {
          const failedName = keys.find((k) => k.id === keyId)?.name
          return (
            <p key={keyId} role="alert" className="px-4 py-3 text-body-sm text-destructive">
              {failedName
                ? `Couldn't revoke "${failedName}" — it is still active. `
                : "Couldn't revoke the key — it is still active. "}
              {message}
            </p>
          )
        })}
      </SCard>

      {/* One-time token reveal. Only "Done" closes it: Esc or a stray click
          outside used to discard a token the server never returns again. */}
      <Dialog open={revealed != null}>
        <DialogContent
          showCloseButton={false}
          onEscapeKeyDown={(event) => event.preventDefault()}
          onInteractOutside={(event) => event.preventDefault()}
        >
          <DialogHeader>
            <DialogTitle>Copy your API key now</DialogTitle>
            <DialogDescription>
              This token is shown only once. Copy it now and store it somewhere safe.
            </DialogDescription>
            {/* Which key this is, while the overlay hides its row. */}
            {revealed && (
              <p className="m-0 text-body-sm text-fg-secondary">
                {describeRevealedKey(
                  revealed,
                  revealed.project_id ? projectNameById[revealed.project_id] : undefined,
                )}
              </p>
            )}
          </DialogHeader>
          <div className="space-y-2 py-2">
            <div className="flex items-center gap-2">
              <input
                ref={tokenRef}
                readOnly
                aria-label="API key"
                value={revealed?.token ?? ''}
                onFocus={(e) => e.currentTarget.select()}
                className="mono h-9 min-w-0 flex-1 rounded-md border px-2 text-body-sm border-border bg-background text-fg"
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => {
                  if (revealed) void copy(revealed.token)
                }}
              >
                <Copy aria-hidden="true" className="h-3.5 w-3.5" />
                {copyState === 'copied' ? 'Copied' : 'Copy'}
              </Button>
            </div>
            <div aria-live="polite" aria-atomic="true">
              {copyState === 'copied' && (
                <p className="text-caption text-success">
                  Copied to the clipboard.
                </p>
              )}
            </div>
            {copyState === 'failed' && (
              <p role="alert" className="text-caption text-danger">
                Couldn’t reach the clipboard. The key above is selected — press Ctrl/⌘+C to copy it.
              </p>
            )}
            <p className="m-0 text-caption text-fg-tertiary">
              Send it as <code className="mono">Authorization: Bearer &lt;key&gt;</code>.
            </p>
          </div>
          <DialogFooter>
            <Button
              onClick={() => {
                setRevealed(null)
                resetCopy()
              }}
            >
              {/* Until Copy has worked, closing is a claim the reader makes
                  about a token that is never shown again. */}
              {copyState === 'copied' ? 'Done' : 'I’ve saved it'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
