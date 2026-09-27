import { useState, type FormEvent } from 'react'
import { Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { ApiError } from '@/api/client'
import { checkDocPath, newDocTemplate } from '@/lib/docTree'
import { getErrorMessage } from '@/lib/utils'
import type { DocScope, DocWriteResponse } from '@/types/docs'
import { useMoveDoc, useWriteDoc } from './useDocs'

export interface NewDocRequest {
  scope: DocScope
  /** Folder prefix with a trailing slash, or `''`. */
  folder: string
  /** Body text under the heading, e.g. a `[[event:…]]` link. */
  body?: string
}

/**
 * "New note". Folders are implicit, so a new folder is simply a path with a
 * `/` in it: `guides/setup` creates `guides/setup.md` and the folder with it.
 * The note is created at once (create-only) and opened in the editor.
 */
export function NewDocDialog({
  slug,
  request,
  organizationName,
  canWriteOrganization = true,
  onClose,
  onCreated,
}: {
  slug: string
  request: NewDocRequest | null
  organizationName: string
  /** Organization notes are written by organization owners and admins only. */
  canWriteOrganization?: boolean
  onClose: () => void
  onCreated: (doc: DocWriteResponse) => void
}) {
  return (
    <Dialog open={request !== null} onOpenChange={open => { if (!open) onClose() }}>
      {request && (
        <NewDocForm
          key={`${request.scope}:${request.folder}:${request.body ?? ''}`}
          slug={slug}
          request={request}
          organizationName={organizationName}
          canWriteOrganization={canWriteOrganization}
          onClose={onClose}
          onCreated={onCreated}
        />
      )}
    </Dialog>
  )
}

function NewDocForm({
  slug,
  request,
  organizationName,
  canWriteOrganization,
  onClose,
  onCreated,
}: {
  slug: string
  request: NewDocRequest
  organizationName: string
  canWriteOrganization: boolean
  onClose: () => void
  onCreated: (doc: DocWriteResponse) => void
}) {
  const [scope, setScope] = useState<DocScope>(canWriteOrganization ? request.scope : 'project')
  const [path, setPath] = useState(request.folder)
  const [error, setError] = useState<string | null>(null)
  const write = useWriteDoc(slug)
  const check = checkDocPath(path)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!check.ok) {
      setError(check.error)
      return
    }
    setError(null)
    try {
      const created = await write.mutateAsync({
        scope,
        path: check.path,
        body: { content: newDocTemplate(check.path, { body: request.body }), create_only: true, message: 'Created' },
      })
      onCreated(created)
    } catch (err) {
      // A 409 names the note already at that path (paths are case-insensitive).
      setError(getErrorMessage(err))
    }
  }

  return (
    <DialogContent
      // Start in the path field, not on the scope toggle above it.
      onOpenAutoFocus={e => {
        e.preventDefault()
        document.getElementById('new-doc-path')?.focus()
      }}
    >
      <form onSubmit={e => void submit(e)} className="flex min-h-0 flex-col gap-4">
        <DialogHeader>
          <DialogTitle>New note</DialogTitle>
          <DialogDescription>
            Use "/" for folders — <span className="mono">guides/checkout.md</span> creates the folder too.
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <span className="text-body-sm font-medium">Where</span>
            <SegmentedControl<DocScope>
              aria-label="Scope"
              value={scope}
              onChange={value => setScope(value)}
              options={[
                { value: 'project', label: 'This project' },
                {
                  value: 'organization',
                  label: `Organization · ${organizationName}`,
                  disabled: !canWriteOrganization,
                  title: canWriteOrganization ? undefined : 'Only organization owners and admins write organization notes',
                },
              ]}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="new-doc-path">Path</Label>
            <Input
              id="new-doc-path"
              value={path}
              onChange={e => setPath(e.target.value)}
              placeholder="guides/checkout.md"
              className="mono"
              aria-invalid={error !== null}
              aria-describedby="new-doc-path-hint"
            />
            <p id="new-doc-path-hint" className="m-0 text-caption text-fg-tertiary">
              {check.ok ? <>Creates <span className="mono">{check.path}</span></> : path ? check.error : '.md is added when missing.'}
            </p>
          </div>
          {error && (
            <p role="alert" className="m-0 text-body-sm text-danger">
              {error}
            </p>
          )}
        </DialogBody>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={write.isPending || !check.ok}>
            {write.isPending && <Loader2 className="animate-spin" aria-hidden />}
            Create
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  )
}

export interface MoveRequest {
  scope: DocScope
  /** A note path, or a folder prefix with a trailing slash when `folder`. */
  from: string
  folder: boolean
}

/** Rename or move a note, or a whole folder (a prefix move, all-or-nothing). */
export function MoveDocDialog({
  slug,
  request,
  onClose,
  onMoved,
}: {
  slug: string
  request: MoveRequest | null
  onClose: () => void
  onMoved: (moved: { from_path: string; to_path: string }[], request: MoveRequest) => void
}) {
  return (
    <Dialog open={request !== null} onOpenChange={open => { if (!open) onClose() }}>
      {request && (
        <MoveForm
          key={`${request.scope}:${request.from}`}
          slug={slug}
          request={request}
          onClose={onClose}
          onMoved={onMoved}
        />
      )}
    </Dialog>
  )
}

function MoveForm({
  slug,
  request,
  onClose,
  onMoved,
}: {
  slug: string
  request: MoveRequest
  onClose: () => void
  onMoved: (moved: { from_path: string; to_path: string }[], request: MoveRequest) => void
}) {
  const [target, setTarget] = useState(request.from)
  const [error, setError] = useState<string | null>(null)
  const move = useMoveDoc(slug)
  const check = checkDocPath(target, { asFolder: request.folder })
  const unchanged = check.ok && check.path === request.from
  const noun = request.folder ? 'folder' : 'note'

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!check.ok || unchanged) return
    setError(null)
    try {
      const result = await move.mutateAsync({
        scope: request.scope,
        from_path: request.from,
        to_path: check.path,
        folder: request.folder,
      })
      onMoved(result.moved, request)
    } catch (err) {
      setError(moveErrorMessage(err))
    }
  }

  return (
    <DialogContent>
      <form onSubmit={e => void submit(e)} className="flex min-h-0 flex-col gap-4">
        <DialogHeader>
          <DialogTitle>Rename or move {noun}</DialogTitle>
          <DialogDescription>
            {request.folder
              ? 'Every note under this folder moves with it. Nothing moves if any target path is taken.'
              : 'Links written as [[event:…]] keep working; relative links from other notes to this one do not follow it.'}
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="flex flex-col gap-1.5">
          <Label htmlFor="move-doc-path">New {request.folder ? 'folder path' : 'path'}</Label>
          <Input
            id="move-doc-path"
            value={target}
            onChange={e => setTarget(e.target.value)}
            className="mono"
            aria-invalid={!check.ok || error !== null}
          />
          {!check.ok && <p className="m-0 text-caption text-danger">{check.error}</p>}
          {error && (
            <p role="alert" className="m-0 whitespace-pre-wrap text-body-sm text-danger">
              {error}
            </p>
          )}
        </DialogBody>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={move.isPending || !check.ok || unchanged}>
            {move.isPending && <Loader2 className="animate-spin" aria-hidden />}
            Move
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  )
}

/** A 409 from move carries the colliding paths; name them. */
function moveErrorMessage(err: unknown): string {
  if (err instanceof ApiError && err.status === 409 && err.detail && typeof err.detail === 'object') {
    const detail = err.detail as { message?: unknown; collisions?: unknown; paths?: unknown }
    const paths = Array.isArray(detail.collisions) ? detail.collisions : Array.isArray(detail.paths) ? detail.paths : []
    const lead = typeof detail.message === 'string' ? detail.message : 'These paths are already taken'
    if (paths.length > 0) return `${lead}:\n${paths.map(String).join('\n')}`
  }
  return getErrorMessage(err)
}
