import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Building2,
  ChevronRight,
  FilePlus2,
  FileText,
  Folder,
  FolderOpen,
  FolderPen,
  FolderX,
  Lock,
  Search,
  Share2,
  Users,
} from 'lucide-react'
import { Input } from '@/components/ui/input'
import { IconButton } from '@/components/ui/icon-button'
import { ancestorFolders, buildDocTree, docMatchScore, docRoute, type DocTreeFolder } from '@/lib/docTree'
import { cn } from '@/lib/utils'
import type { DocScope, DocSummary, DocTreeResponse } from '@/types/docs'

export interface FolderActions {
  onNewInFolder: (scope: DocScope, prefix: string) => void
  onMoveFolder: (scope: DocScope, prefix: string) => void
  onDeleteFolder: (scope: DocScope, prefix: string, count: number) => void
  /** Who can read the folder's notes (F24); absent hides the menu item. */
  onShareFolder?: (scope: DocScope, prefix: string) => void
  /**
   * Whether the viewer may change notes of `scope`: project notes follow the
   * project's write gate, organization notes need an organization owner or admin.
   */
  canEditScope: (scope: DocScope) => boolean
}

/**
 * The left-hand tree (F22): a root per scope, folders built from the flat
 * paths, a filter that fuzzy-matches titles and paths. Folder actions show
 * for editors only (`actions` undefined for a reader).
 */
export function DocsTree({
  slug,
  tree,
  active,
  actions,
}: {
  slug: string
  tree: DocTreeResponse
  active: { scope: DocScope; path: string } | null
  actions?: FolderActions
}) {
  const [filter, setFilter] = useState('')
  const roots: { scope: DocScope; label: string; docs: DocSummary[] }[] = [
    { scope: 'project', label: 'Project notes', docs: tree.project_docs },
    { scope: 'organization', label: `Organization notes · ${tree.organization.name}`, docs: tree.organization_docs },
  ]

  return (
    <nav aria-label="Docs" className="flex min-w-0 flex-col gap-3">
      <div className="relative">
        <Search className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-fg-tertiary" aria-hidden />
        <Input
          type="search"
          value={filter}
          onChange={e => setFilter(e.target.value)}
          placeholder="Filter notes"
          aria-label="Filter notes"
          className="h-8 pl-7"
        />
      </div>
      {roots.map(root => (
        <ScopeRoot
          key={root.scope}
          slug={slug}
          scope={root.scope}
          label={root.label}
          docs={root.docs}
          filter={filter}
          active={active?.scope === root.scope ? active.path : null}
          actions={actions}
        />
      ))}
    </nav>
  )
}

function ScopeRoot({
  slug,
  scope,
  label,
  docs,
  filter,
  active,
  actions,
}: {
  slug: string
  scope: DocScope
  label: string
  docs: DocSummary[]
  filter: string
  active: string | null
  actions?: FolderActions
}) {
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set())
  const filtering = filter.trim() !== ''
  const visible = useMemo(
    () => (filtering ? docs.filter(doc => docMatchScore(filter, doc) !== null) : docs),
    [docs, filter, filtering],
  )
  const root = useMemo(() => buildDocTree(visible), [visible])
  // Opening a note expands its folders once; after that they collapse like any
  // other (an ancestor forced open on every render made the toggle a no-op).
  const [expandedFor, setExpandedFor] = useState<string | null>(null)
  if (active !== expandedFor) {
    setExpandedFor(active)
    const ancestors = active ? ancestorFolders(active) : []
    if (ancestors.some(prefix => collapsed.has(prefix))) {
      setCollapsed(prev => {
        const next = new Set(prev)
        for (const prefix of ancestors) next.delete(prefix)
        return next
      })
    }
  }
  // While filtering every folder is shown open so no match is hidden; the
  // toggle says so (aria-disabled) instead of silently doing nothing.
  const isOpen = (prefix: string) => filtering || !collapsed.has(prefix)
  const toggle = (prefix: string) => {
    if (filtering) return
    setCollapsed(prev => {
      const next = new Set(prev)
      if (next.has(prefix)) next.delete(prefix)
      else next.add(prefix)
      return next
    })
  }

  const headingId = `docs-root-${scope}`
  return (
    <section aria-labelledby={headingId} className="min-w-0">
      <div className="flex items-center gap-1.5 px-1 pb-1">
        {scope === 'organization' ? (
          <Building2 className="size-3.5 shrink-0 text-fg-tertiary" aria-hidden />
        ) : (
          <FileText className="size-3.5 shrink-0 text-fg-tertiary" aria-hidden />
        )}
        <h2 id={headingId} className="micro-label m-0 min-w-0 flex-1 truncate text-fg-tertiary">
          {label}
        </h2>
        <span className="tnum text-caption text-fg-faint">{docs.length}</span>
        {actions?.canEditScope(scope) && (
          <IconButton
            label={scope === 'project' ? 'New project note' : 'New organization note'}
            size="icon-xs"
            variant="ghost"
            onClick={() => actions.onNewInFolder(scope, '')}
          >
            <FilePlus2 />
          </IconButton>
        )}
      </div>
      {visible.length === 0 ? (
        <p className="m-0 px-2 py-1 text-caption text-fg-tertiary">
          {filtering ? 'No match.' : 'No notes yet.'}
        </p>
      ) : (
        <FolderList
          slug={slug}
          scope={scope}
          folder={root}
          depth={0}
          active={active}
          isOpen={isOpen}
          toggle={toggle}
          toggleLocked={filtering}
          actions={actions}
        />
      )}
    </section>
  )
}

function FolderList({
  slug,
  scope,
  folder,
  depth,
  active,
  isOpen,
  toggle,
  toggleLocked,
  actions,
}: {
  slug: string
  scope: DocScope
  folder: DocTreeFolder<DocSummary>
  depth: number
  active: string | null
  isOpen: (prefix: string) => boolean
  toggle: (prefix: string) => void
  /** True while a filter is on: folders stay open so no match is hidden. */
  toggleLocked: boolean
  actions?: FolderActions
}) {
  const indent = { paddingLeft: `${depth * 12 + 4}px` }
  return (
    <ul className="m-0 list-none p-0">
      {folder.folders.map(sub => {
        const open = isOpen(sub.path)
        return (
          <li key={sub.path}>
            <div className="group flex items-center rounded-control hover:bg-surface-hover" style={indent}>
              <button
                type="button"
                aria-expanded={open}
                aria-disabled={toggleLocked || undefined}
                title={toggleLocked ? 'Clear the filter to collapse folders' : undefined}
                onClick={() => toggle(sub.path)}
                className="flex min-w-0 flex-1 items-center gap-1 py-1 text-left text-body-sm text-fg-secondary"
              >
                <ChevronRight className={cn('size-3 shrink-0 transition-transform', open && 'rotate-90')} aria-hidden />
                {open ? (
                  <FolderOpen className="size-3.5 shrink-0 text-fg-tertiary" aria-hidden />
                ) : (
                  <Folder className="size-3.5 shrink-0 text-fg-tertiary" aria-hidden />
                )}
                <span className="truncate">{sub.name}</span>
                <span className="tnum ml-1 text-caption text-fg-faint">{sub.count}</span>
              </button>
              {actions?.canEditScope(scope) && (
                <span className="hidden shrink-0 items-center group-focus-within:flex group-hover:flex">
                  <IconButton label={`New note in ${sub.path}`} size="icon-xs" variant="ghost" onClick={() => actions.onNewInFolder(scope, sub.path)}>
                    <FilePlus2 />
                  </IconButton>
                  <IconButton label={`Rename or move ${sub.path}`} size="icon-xs" variant="ghost" onClick={() => actions.onMoveFolder(scope, sub.path)}>
                    <FolderPen />
                  </IconButton>
                  {actions.onShareFolder && (
                    <IconButton label={`Share ${sub.path}`} size="icon-xs" variant="ghost" onClick={() => actions.onShareFolder?.(scope, sub.path)}>
                      <Share2 />
                    </IconButton>
                  )}
                  <IconButton label={`Delete ${sub.path}`} size="icon-xs" variant="ghost" onClick={() => actions.onDeleteFolder(scope, sub.path, sub.count)}>
                    <FolderX />
                  </IconButton>
                </span>
              )}
            </div>
            {open && (
              <FolderList
                slug={slug}
                scope={scope}
                folder={sub}
                depth={depth + 1}
                active={active}
                isOpen={isOpen}
                toggle={toggle}
                toggleLocked={toggleLocked}
                actions={actions}
              />
            )}
          </li>
        )
      })}
      {folder.files.map(file => {
        const selected = active !== null && file.path.toLowerCase() === active.toLowerCase()
        return (
          <li key={file.path}>
            <Link
              to={docRoute(slug, scope, file.path)}
              aria-current={selected ? 'page' : undefined}
              title={file.path}
              aria-label={`${file.doc.title || file.name}${visibilitySuffix(file.doc)}`}
              className={cn(
                'flex min-w-0 items-center gap-1.5 rounded-control py-1 pr-1 text-body-sm hover:bg-surface-hover',
                selected ? 'bg-surface-active font-medium text-fg' : 'text-fg-secondary',
              )}
              style={{ paddingLeft: `${depth * 12 + 20}px` }}
            >
              <FileText className="size-3.5 shrink-0 text-fg-tertiary" aria-hidden />
              <span className="truncate">{file.doc.title || file.name}</span>
              <VisibilityIcon doc={file.doc} />
            </Link>
          </li>
        )
      })}
    </ul>
  )
}

/**
 * What the row's icon says, for its accessible name. The link carries it as an
 * aria-label: an sr-only span inside the link would lose the separating space.
 */
function visibilitySuffix(doc: DocSummary): string {
  if (doc.visibility === 'private') return ' (only the author)'
  if (doc.visibility === 'restricted') return ' (shared with specific people)'
  return ''
}

/** Lock for an author-only note, people for a shared one; nothing for the default. */
function VisibilityIcon({ doc }: { doc: DocSummary }) {
  if (doc.visibility === 'private') {
    return (
      <span className="ml-auto shrink-0 text-fg-tertiary" title="Only the author">
        <Lock className="size-3" aria-hidden />
      </span>
    )
  }
  if (doc.visibility === 'restricted') {
    return (
      <span className="ml-auto shrink-0 text-fg-tertiary" title="Shared with specific people">
        <Users className="size-3" aria-hidden />
      </span>
    )
  }
  return null
}
