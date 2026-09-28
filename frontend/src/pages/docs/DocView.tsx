import { toast } from 'sonner'
import { AlertTriangle, Copy, FolderInput, History, Lock, Pencil, Share2, Trash2, Users } from 'lucide-react'
import { Chip } from '@/components/primitives/chip'
import { Button } from '@/components/ui/button'
import { IconButton } from '@/components/ui/icon-button'
import { useCopyToClipboard } from '@/hooks/useCopyToClipboard'
import { describeUnresolved } from '@/lib/docLinks'
import { formatBytes } from '@/lib/docTree'
import { formatDateTime } from '@/lib/datetime'
import type { DocAudience, DocFileResponse } from '@/types/docs'
import { DocMarkdown } from './DocMarkdown'

const AUDIENCE_LABEL: Record<DocAudience, string> = {
  human: 'For people',
  agent: 'For agents',
  both: 'For people and agents',
}

/** One note, rendered: title, frontmatter chips, broken-link banner, body. */
export function DocView({
  slug,
  doc,
  canEdit,
  onEdit,
  onHistory,
  onMove,
  onDelete,
  onShare,
}: {
  slug: string
  doc: DocFileResponse
  canEdit: boolean
  onEdit: () => void
  onHistory: () => void
  onMove: () => void
  onDelete: () => void
  /** Opens the Share dialog (F24); anyone who reads the note sees who else can. */
  onShare?: () => void
}) {
  const { copy } = useCopyToClipboard()
  const unresolved = doc.links.filter(link => link.status !== 'resolved')
  const extraKeys = Object.keys(doc.extra_frontmatter)

  return (
    <article aria-labelledby="doc-title" className="flex min-w-0 flex-col gap-4">
      <header className="flex flex-col gap-2 border-b border-border pb-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <h2 id="doc-title" className="m-0 break-words text-title font-semibold tracking-[-0.01em]">
              {doc.title}
            </h2>
            <div className="mono mt-1 flex min-w-0 items-center gap-1 text-caption text-fg-tertiary">
              <span className="truncate">{doc.path}</span>
              <IconButton
                label="Copy path"
                size="icon-xs"
                variant="ghost"
                onClick={() => {
                  void copy(doc.path).then(ok => {
                    if (ok) toast.success('Path copied')
                  })
                }}
              >
                <Copy />
              </IconButton>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            {onShare && (
              <Button variant="outline" size="sm" onClick={onShare}>
                <Share2 aria-hidden />
                Share
              </Button>
            )}
            <Button variant="outline" size="sm" onClick={onHistory}>
              <History aria-hidden />
              History
            </Button>
            {canEdit && (
              <>
                <IconButton label="Rename or move" size="icon-sm" variant="ghost" onClick={onMove}>
                  <FolderInput />
                </IconButton>
                <IconButton label="Delete note" size="icon-sm" variant="danger" onClick={onDelete}>
                  <Trash2 />
                </IconButton>
                <Button size="sm" onClick={onEdit}>
                  <Pencil aria-hidden />
                  Edit
                </Button>
              </>
            )}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Chip size="xs" variant="outline">
            {doc.scope === 'organization' ? 'Organization note' : 'Project note'}
          </Chip>
          <VisibilityChip doc={doc} />
          <Chip size="xs" tone={doc.audience === 'agent' ? 'info' : doc.audience === 'human' ? 'accent' : 'neutral'}>
            {AUDIENCE_LABEL[doc.audience]}
          </Chip>
          {doc.tags.map(tag => (
            <Chip key={tag} size="xs" variant="outline" className="mono">
              #{tag}
            </Chip>
          ))}
          <span className="text-caption text-fg-tertiary">
            Revision {doc.revision} · {formatBytes(doc.size_bytes)} · updated {formatDateTime(doc.updated_at)}
            {doc.updated_by_name ? ` by ${doc.updated_by_name}` : ''}
          </span>
        </div>
        {doc.description && <p className="m-0 text-body text-fg-secondary">{doc.description}</p>}
      </header>

      {doc.break_glass && <BreakGlassBanner />}

      {unresolved.length > 0 && <BrokenLinksBanner items={unresolved.map(describeUnresolved)} />}

      <DocMarkdown body={doc.body} slug={slug} scope={doc.scope} path={doc.path} resolutions={doc.links} />

      {extraKeys.length > 0 && (
        <details className="rounded-control border border-border bg-bg-sunken px-3 py-2 text-body-sm">
          <summary className="cursor-pointer text-fg-secondary">
            Other frontmatter ({extraKeys.length} {extraKeys.length === 1 ? 'key' : 'keys'})
          </summary>
          <pre className="mono m-0 mt-2 overflow-x-auto whitespace-pre-wrap text-caption">
            {JSON.stringify(doc.extra_frontmatter, null, 2)}
          </pre>
        </details>
      )}
    </article>
  )
}

/**
 * Who can read the note (F24): nothing for the default (everyone in the
 * project or organization), a lock for the author only, people for shared.
 */
export function VisibilityChip({ doc }: { doc: Pick<DocFileResponse, 'visibility' | 'shared' | 'my_permission'> }) {
  const readOnly = doc.my_permission === 'view' ? ' · view only' : ''
  if (doc.visibility === 'private') {
    return (
      <Chip size="xs" tone="warning" icon={<Lock className="size-3" aria-hidden />}>
        Only the author{readOnly}
      </Chip>
    )
  }
  if (doc.visibility === 'restricted') {
    return (
      <Chip size="xs" tone="info" icon={<Users className="size-3" aria-hidden />}>
        Shared with specific people{readOnly}
      </Chip>
    )
  }
  return readOnly ? (
    <Chip size="xs" variant="outline">
      View only
    </Chip>
  ) : null
}

/** An org owner/admin reading a note that is not shared with them (F24). */
export function BreakGlassBanner() {
  return (
    <div role="status" className="rounded-control border border-warning bg-warning-soft px-3 py-2 text-body-sm">
      <p className="m-0 flex items-center gap-1.5 font-medium text-warning">
        <Lock className="size-3.5" aria-hidden />
        This note is not shared with you
      </p>
      <p className="m-0 mt-1 text-fg-secondary">
        You can read it because you are an organization owner or admin. This read was recorded in the audit
        log, and you cannot edit the note unless it is shared with you for editing.
      </p>
    </div>
  )
}

export function BrokenLinksBanner({ items }: { items: readonly string[] }) {
  return (
    <div role="status" className="rounded-control border border-warning bg-warning-soft px-3 py-2 text-body-sm">
      <p className="m-0 flex items-center gap-1.5 font-medium text-warning">
        <AlertTriangle className="size-3.5" aria-hidden />
        {items.length === 1 ? '1 link does not resolve' : `${items.length} links do not resolve`}
      </p>
      <ul className="m-0 mt-1 list-disc pl-6 text-fg-secondary">
        {items.slice(0, 20).map(item => (
          <li key={item} className="mono text-caption">
            {item}
          </li>
        ))}
        {items.length > 20 && <li className="text-caption">…and {items.length - 20} more</li>}
      </ul>
    </div>
  )
}
