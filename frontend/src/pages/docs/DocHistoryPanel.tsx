import { useState } from 'react'
import { toast } from 'sonner'
import { ArrowLeft, RotateCcw } from 'lucide-react'
import { Chip } from '@/components/primitives/chip'
import { LoadingState } from '@/components/primitives/loading-state'
import { ErrorState } from '@/components/error-state'
import { Button } from '@/components/ui/button'
import {
  Sheet,
  SheetBody,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { useConfirm } from '@/hooks/useConfirm'
import { formatDateTime } from '@/lib/datetime'
import { formatBytes } from '@/lib/docTree'
import { cn } from '@/lib/utils'
import type { DocRevisionAction, DocRevisionSummary, DocScope } from '@/types/docs'
import { useDocRevision, useDocRevisions, useRestoreDocRevision } from './useDocs'

const ACTION_LABEL: Record<DocRevisionAction, string> = {
  create: 'Created',
  update: 'Edited',
  move: 'Moved',
  restore: 'Restored',
  import: 'Imported',
}

/**
 * The History drawer (F22): every revision of one note with its author and
 * time; picking one shows its unified diff against the previous revision and,
 * for an editor, Restore — which writes a NEW revision, so nothing is lost.
 */
export function DocHistoryPanel({
  slug,
  scope,
  path,
  open,
  onOpenChange,
  canEdit,
}: {
  slug: string
  scope: DocScope
  path: string
  open: boolean
  onOpenChange: (open: boolean) => void
  canEdit: boolean
}) {
  const [selected, setSelected] = useState<string | null>(null)
  const list = useDocRevisions(slug, scope, path, open)

  return (
    <Sheet
      open={open}
      onOpenChange={next => {
        if (!next) setSelected(null)
        onOpenChange(next)
      }}
    >
      <SheetContent side="right" className="w-[min(560px,100vw)]">
        <SheetHeader>
          <SheetTitle>History</SheetTitle>
          <SheetDescription className="mono truncate">{path}</SheetDescription>
        </SheetHeader>
        <SheetBody>
          {selected ? (
            <RevisionDetail
              slug={slug}
              revisionId={selected}
              currentRevision={list.data?.current_revision ?? null}
              canEdit={canEdit}
              onBack={() => setSelected(null)}
              onRestored={() => {
                setSelected(null)
                onOpenChange(false)
              }}
            />
          ) : list.isPending ? (
            <LoadingState label="Loading history…" />
          ) : list.isError ? (
            <ErrorState compact title="Couldn't load the history" error={list.error} onRetry={() => void list.refetch()} />
          ) : (
            <ol className="m-0 flex list-none flex-col gap-1 p-0">
              {list.data.items.map(item => (
                <li key={item.id}>
                  <RevisionRow
                    item={item}
                    current={item.number === list.data.current_revision}
                    onOpen={() => setSelected(item.id)}
                  />
                </li>
              ))}
            </ol>
          )}
        </SheetBody>
      </SheetContent>
    </Sheet>
  )
}

function RevisionRow({ item, current, onOpen }: { item: DocRevisionSummary; current: boolean; onOpen: () => void }) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className="flex w-full flex-col gap-0.5 rounded-control px-2 py-1.5 text-left hover:bg-surface-hover"
    >
      <span className="flex flex-wrap items-center gap-1.5 text-body-sm">
        <span className="tnum font-medium">#{item.number}</span>
        <span>{ACTION_LABEL[item.action]}</span>
        {item.restored_from_number !== null && (
          <span className="text-fg-tertiary">from #{item.restored_from_number}</span>
        )}
        {current && (
          <Chip size="xs" tone="accent">
            Current
          </Chip>
        )}
      </span>
      <span className="text-caption text-fg-tertiary">
        {item.author_name ?? 'Unknown'} · {formatDateTime(item.created_at)} · {formatBytes(item.size_bytes)}
      </span>
      {item.message && <span className="text-body-sm text-fg-secondary">{item.message}</span>}
      {item.action === 'move' && <span className="mono text-caption text-fg-tertiary">{item.path}</span>}
    </button>
  )
}

function RevisionDetail({
  slug,
  revisionId,
  currentRevision,
  canEdit,
  onBack,
  onRestored,
}: {
  slug: string
  revisionId: string
  currentRevision: number | null
  canEdit: boolean
  onBack: () => void
  onRestored: () => void
}) {
  const detail = useDocRevision(slug, revisionId)
  const restore = useRestoreDocRevision(slug)
  const { confirm, dialog } = useConfirm()

  if (detail.isPending) return <LoadingState label="Loading revision…" />
  if (detail.isError) return <ErrorState compact title="Couldn't load this revision" error={detail.error} />
  const rev = detail.data
  const isCurrent = rev.number === currentRevision

  const onRestore = async () => {
    const ok = await confirm({
      title: `Restore revision #${rev.number}?`,
      message: 'The note goes back to this content as a new revision. Nothing in its history is lost.',
      confirmLabel: 'Restore',
      variant: 'primary',
      action: () => restore.mutateAsync({ revisionId: rev.id, message: `Restored revision ${rev.number}` }),
      errorPrefix: 'Could not restore',
      pendingLabel: 'Restoring…',
    })
    if (ok) {
      toast.success(`Restored revision #${rev.number}`)
      onRestored()
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="ghost" size="sm" onClick={onBack}>
          <ArrowLeft aria-hidden />
          All revisions
        </Button>
        <span className="flex-1" />
        {canEdit && !isCurrent && (
          <Button size="sm" onClick={() => void onRestore()}>
            <RotateCcw aria-hidden />
            Restore
          </Button>
        )}
      </div>
      <div className="text-body-sm">
        <span className="font-medium">
          #{rev.number} · {ACTION_LABEL[rev.action]}
        </span>{' '}
        <span className="text-fg-tertiary">
          by {rev.author_name ?? 'Unknown'}, {formatDateTime(rev.created_at)}
        </span>
        {rev.message && <p className="m-0 mt-1 text-fg-secondary">{rev.message}</p>}
      </div>
      <h3 className="micro-label m-0 text-fg-tertiary">
        {rev.diff ? 'Changes from the previous revision' : 'Content'}
      </h3>
      {rev.diff ? <DiffView diff={rev.diff} /> : <pre className={PRE_CLASS}>{rev.content}</pre>}
      {rev.diff_truncated && (
        <p className="m-0 text-caption text-fg-tertiary">The diff was too long and is cut short.</p>
      )}
      {dialog}
    </div>
  )
}

const PRE_CLASS =
  'mono m-0 max-h-[60vh] overflow-auto whitespace-pre-wrap break-words rounded-control border border-border bg-bg-sunken p-2 text-caption'

/** A unified diff with added and removed lines tinted. */
export function DiffView({ diff }: { diff: string }) {
  const lines = diff.split('\n')
  return (
    <pre className={PRE_CLASS} aria-label="Unified diff">
      {lines.map((line, i) => (
        <span
          key={i}
          className={cn(
            'block',
            line.startsWith('+') && !line.startsWith('+++') && 'bg-success-soft text-success',
            line.startsWith('-') && !line.startsWith('---') && 'bg-danger-soft text-danger',
            line.startsWith('@@') && 'text-fg-tertiary',
          )}
        >
          {line || ' '}
        </span>
      ))}
    </pre>
  )
}
