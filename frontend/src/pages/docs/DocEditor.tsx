import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import CodeMirror, { EditorView } from '@uiw/react-codemirror'
import { markdown } from '@codemirror/lang-markdown'
import type { BlockerFunction } from 'react-router-dom'
import { toast } from 'sonner'
import { Loader2, Save } from 'lucide-react'
import { ApiError } from '@/api/client'
import { docsApi } from '@/api/docs'
import { useTheme } from '@/components/theme-provider'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { useUnsavedChangesGuard } from '@/hooks/useUnsavedChangesGuard'
import { describeUnresolved } from '@/lib/docLinks'
import { splitFrontmatter } from '@/lib/docFrontmatter'
import { formatBytes } from '@/lib/docTree'
import { cn, getErrorMessage } from '@/lib/utils'
import type { DocFileResponse, DocWriteResponse } from '@/types/docs'
import { DocMarkdown } from './DocMarkdown'
import { BrokenLinksBanner } from './DocView'
import { useDraftLinkResolutions, useWriteDoc } from './useDocs'

const encoder = new TextEncoder()

/**
 * Edit mode is `?edit=1` on the note's own path, so the router's path check
 * alone would let a click on the open note in the tree (same path, no `?edit`)
 * unmount the editor and drop the draft without asking.
 */
const leavesEditMode: BlockerFunction = ({ currentLocation, nextLocation }) =>
  new URLSearchParams(currentLocation.search).get('edit') === '1'
  && new URLSearchParams(nextLocation.search).get('edit') !== '1'

/**
 * The Markdown editor (F22): CodeMirror on the left, the rendered preview on
 * the right with `[[…]]` links resolved live (debounced). Saving sends the
 * revision the draft started from, so a concurrent edit comes back as a 409
 * and a choice — reload theirs, or overwrite with mine — instead of a silent
 * last-write-wins.
 */
export function DocEditor({
  slug,
  doc,
  maxBytes,
  onDone,
}: {
  slug: string
  doc: DocFileResponse
  maxBytes: number
  /** Called after a save (with the response) or a cancel (with null). */
  onDone: (saved: DocWriteResponse | null) => void
}) {
  const [draft, setDraft] = useState(doc.content)
  const [baseRevision, setBaseRevision] = useState(doc.revision)
  const [message, setMessage] = useState('')
  const [conflict, setConflict] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [baseline, setBaseline] = useState(doc.content)
  const write = useWriteDoc(slug)
  const dirty = draft !== baseline
  const guard = useUnsavedChangesGuard(dirty, { alsoBlock: leavesEditMode })

  const { resolvedTheme } = useTheme()
  const extensions = useMemo(() => [markdown(), EditorView.lineWrapping], [])
  const size = useMemo(() => encoder.encode(draft).length, [draft])
  const tooBig = size > maxBytes
  const previewBody = useMemo(() => splitFrontmatter(draft).body, [draft])
  const links = useDraftLinkResolutions(slug, draft)
  const unresolved = (links.data ?? []).filter(link => link.status !== 'resolved')

  const save = useCallback(
    async (base: number) => {
      if (busy || tooBig) return
      setBusy(true)
      setError(null)
      try {
        const saved = await write.mutateAsync({
          scope: doc.scope,
          path: doc.path,
          body: { content: draft, base_revision: base, message: message.trim() },
        })
        guard.release()
        setBaseline(draft)
        toast.success(saved.changed ? `Saved revision ${saved.revision}` : 'No changes to save')
        if (saved.warnings.length > 0) {
          toast.warning(
            saved.warnings.length === 1 ? saved.warnings[0] : `${saved.warnings.length} links do not resolve`,
          )
        }
        onDone(saved)
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) setConflict(true)
        else setError(getErrorMessage(err))
      } finally {
        setBusy(false)
      }
    },
    [busy, tooBig, write, doc.scope, doc.path, draft, message, guard, onDone],
  )

  // Ctrl/Cmd+S saves while the editor is open — but not under the conflict
  // dialog, where re-sending the stale base revision would only 409 again.
  const saveRef = useRef(() => void save(baseRevision))
  useEffect(() => {
    saveRef.current = () => {
      if (!conflict) void save(baseRevision)
    }
  }, [save, baseRevision, conflict])
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault()
        saveRef.current()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // A confirmed discard releases the guard first, or the `?edit` blocker
  // would ask a second time as the URL drops edit mode.
  const cancel = () =>
    guard.requestLeave(() => {
      guard.release()
      onDone(null)
    })

  const reloadTheirs = async () => {
    setBusy(true)
    try {
      const latest = await docsApi.read(slug, doc.scope, doc.path)
      setDraft(latest.content)
      setBaseline(latest.content)
      setBaseRevision(latest.revision)
      setConflict(false)
      toast.info(`Loaded revision ${latest.revision}; your edits were discarded`)
    } catch (err) {
      setError(getErrorMessage(err))
      setConflict(false)
    } finally {
      setBusy(false)
    }
  }

  const overwrite = async () => {
    setBusy(true)
    let latestRevision: number
    try {
      latestRevision = (await docsApi.read(slug, doc.scope, doc.path)).revision
    } catch (err) {
      setError(getErrorMessage(err))
      setConflict(false)
      setBusy(false)
      return
    }
    setBusy(false)
    setConflict(false)
    setBaseRevision(latestRevision)
    await save(latestRevision)
  }

  return (
    <section aria-label={`Editing ${doc.path}`} className="flex min-w-0 flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2 border-b border-border pb-3">
        <h2 className="m-0 min-w-0 flex-1 truncate text-heading font-semibold">
          Editing <span className="mono text-fg-secondary">{doc.path}</span>
        </h2>
        <Input
          value={message}
          onChange={e => setMessage(e.target.value.slice(0, 500))}
          placeholder="What changed? (optional)"
          aria-label="Revision message"
          className="h-7 w-64 max-w-full"
        />
        <Button variant="outline" size="sm" onClick={cancel} disabled={busy}>
          Cancel
        </Button>
        <Button size="sm" onClick={() => void save(baseRevision)} disabled={busy || tooBig}>
          {busy ? <Loader2 className="animate-spin" aria-hidden /> : <Save aria-hidden />}
          Save
        </Button>
      </div>
      {error && (
        <p role="alert" className="m-0 whitespace-pre-wrap text-body-sm text-danger">
          Could not save: {error}
        </p>
      )}
      <div className="grid min-w-0 gap-4 lg:grid-cols-2">
        <div className="flex min-w-0 flex-col gap-1">
          {/* `sql-editor` is index.css's token frame for CodeMirror (border,
              focus ring, gutters, tooltips); it is not SQL-specific. */}
          <div className="sql-editor min-w-0">
            <CodeMirror
              value={draft}
              onChange={setDraft}
              extensions={extensions}
              theme={resolvedTheme === 'dark' ? 'dark' : 'light'}
              minHeight="420px"
              maxHeight="70vh"
              aria-label="Markdown source"
              basicSetup={{ foldGutter: false, highlightActiveLine: false }}
            />
          </div>
          <p className={cn('m-0 text-caption', tooBig ? 'text-danger' : 'text-fg-tertiary')}>
            {formatBytes(size)} of {formatBytes(maxBytes)}
            {tooBig ? ' — too large to save' : ''} · Ctrl/⌘+S saves · link with [[event:name]], [[event-type:name]],
            [[field:type/name]]
          </p>
        </div>
        <section className="flex min-w-0 flex-col gap-3" aria-label="Preview">
          <div className="micro-label text-fg-tertiary">Preview</div>
          {unresolved.length > 0 && <BrokenLinksBanner items={unresolved.map(describeUnresolved)} />}
          <div className="max-h-[70vh] overflow-y-auto rounded-control border border-border p-4">
            <DocMarkdown body={previewBody} slug={slug} scope={doc.scope} path={doc.path} resolutions={links.data} />
          </div>
        </section>
      </div>

      <Dialog open={conflict} onOpenChange={open => { if (!open) setConflict(false) }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>This note changed while you were editing</DialogTitle>
            <DialogDescription>
              Someone saved a newer revision of {doc.path} after revision {baseRevision}. Load theirs and discard
              your edits, or save yours over it (their revision stays in the history).
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConflict(false)} disabled={busy}>
              Keep editing
            </Button>
            <Button variant="outline" onClick={() => void reloadTheirs()} disabled={busy}>
              Load theirs
            </Button>
            <Button variant="destructive" onClick={() => void overwrite()} disabled={busy}>
              Overwrite with mine
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {guard.dialog}
    </section>
  )
}
