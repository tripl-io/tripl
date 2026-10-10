import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  DndContext,
  DragOverlay,
  PointerSensor,
  pointerWithin,
  useDndContext,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
  type Announcements,
  type CollisionDetection,
  type DragEndEvent,
  type DragStartEvent,
} from '@dnd-kit/core'
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
import {
  ancestorFolders,
  baseName,
  buildDocTree,
  docMatchScore,
  docRoute,
  dropPath,
  type DocTreeFolder,
} from '@/lib/docTree'
import { cn } from '@/lib/utils'
import type { DocScope, DocSummary, DocTreeResponse } from '@/types/docs'

/** A drag-and-drop move in the tree: always within one scope. */
export interface DropMove {
  scope: DocScope
  /** A note path, or a folder prefix with a trailing slash when `folder`. */
  from: string
  to: string
  folder: boolean
}

export interface FolderActions {
  onNewInFolder: (scope: DocScope, prefix: string) => void
  onMoveFolder: (scope: DocScope, prefix: string) => void
  onDeleteFolder: (scope: DocScope, prefix: string, count: number) => void
  /** Who can read the folder's notes (F24); absent hides the menu item. */
  onShareFolder?: (scope: DocScope, prefix: string) => void
  /** A note or folder dropped on a folder; absent turns dragging off. */
  onDropMove?: (move: DropMove) => void
  /**
   * Whether the viewer may change notes of `scope`: project notes follow the
   * project's write gate, organization notes need an organization owner or admin.
   */
  canEditScope: (scope: DocScope) => boolean
}

/** What a dragged row carries; a drop target carries its scope and prefix. */
interface DragItem {
  scope: DocScope
  path: string
  folder: boolean
}
interface DropTarget {
  scope: DocScope
  prefix: string
}

const dragName = (item: DragItem) => (item.folder ? baseName(item.path.slice(0, -1)) : baseName(item.path))
const targetName = (target: DropTarget) => (target.prefix === '' ? 'the top level' : target.prefix)

/** Where an item lands on a target, or null for a cross-scope or no-op drop. */
function validDrop(item: DragItem | undefined, target: DropTarget | undefined): string | null {
  if (!item || !target || item.scope !== target.scope) return null
  return dropPath(item.path, item.folder, target.prefix)
}

/**
 * Folders nest, so the pointer is inside several targets at once (a folder,
 * its parents, the scope root); the innermost, i.e. the smallest, wins.
 */
const innermostTarget: CollisionDetection = args =>
  pointerWithin(args)
    .map(hit => {
      const rect = args.droppableRects.get(hit.id)
      return { hit, area: rect ? rect.width * rect.height : Number.POSITIVE_INFINITY }
    })
    .sort((a, b) => a.area - b.area)
    .map(({ hit }) => hit)

const announcements: Announcements = {
  onDragStart: ({ active }) => `Picked up ${dragName(active.data.current as DragItem)}.`,
  onDragOver: ({ active, over }) => {
    if (!over) return `${dragName(active.data.current as DragItem)} is not over a folder.`
    const ok = validDrop(active.data.current as DragItem, over.data.current as DropTarget) !== null
    return ok ? `Over ${targetName(over.data.current as DropTarget)}.` : 'Cannot drop here.'
  },
  onDragEnd: ({ active, over }) => {
    const to = validDrop(active.data.current as DragItem, over?.data.current as DropTarget | undefined)
    return to ? `Moving ${dragName(active.data.current as DragItem)} to ${to}.` : 'Not moved.'
  },
  onDragCancel: ({ active }) => `Moving ${dragName(active.data.current as DragItem)} was cancelled.`,
}

/**
 * The left-hand tree (F22): a root per scope, folders built from the flat
 * paths, a filter that fuzzy-matches titles and paths. Folder actions show
 * for editors only (`actions` undefined for a reader). An editor can drag a
 * note or folder onto a folder or a scope root, within that scope (pointer
 * only: the Rename or move dialog is the keyboard path).
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
  const [dragging, setDragging] = useState<DragItem | null>(null)
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 4 } }))
  // The root names the docs use. The organization's name is the heading's
  // hover text, not part of it: in a 260px rail "Organization notes · Default
  // organization" was cut off mid-name with no way to read the rest.
  const roots: { scope: DocScope; label: string; hint: string; docs: DocSummary[] }[] = [
    { scope: 'project', label: 'Project notes', hint: `Notes for ${tree.project.name} only`, docs: tree.project_docs },
    {
      scope: 'organization',
      label: 'Organization notes',
      hint: `Notes for every project in ${tree.organization.name}`,
      docs: tree.organization_docs,
    },
  ]
  // With no notes anywhere the page's own empty state says so once; a "No
  // notes yet." under each root as well said it three times.
  const treeEmpty = tree.project_docs.length + tree.organization_docs.length === 0

  const onDragStart = (event: DragStartEvent) => setDragging(event.active.data.current as DragItem)
  const onDragEnd = (event: DragEndEvent) => {
    setDragging(null)
    const item = event.active.data.current as DragItem
    const to = validDrop(item, event.over?.data.current as DropTarget | undefined)
    if (to) actions?.onDropMove?.({ scope: item.scope, from: item.path, to, folder: item.folder })
  }

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
      <DndContext
        sensors={sensors}
        collisionDetection={innermostTarget}
        onDragStart={onDragStart}
        onDragEnd={onDragEnd}
        onDragCancel={() => setDragging(null)}
        accessibility={{ announcements }}
      >
        {roots.map(root => (
          <ScopeRoot
            key={root.scope}
            slug={slug}
            scope={root.scope}
            label={root.label}
            hint={root.hint}
            docs={root.docs}
            treeEmpty={treeEmpty}
            filter={filter}
            active={active?.scope === root.scope ? active.path : null}
            actions={actions}
          />
        ))}
        <DragOverlay dropAnimation={null}>
          {dragging && (
            <span className="inline-flex max-w-60 items-center gap-1.5 rounded-control border border-border bg-surface px-2 py-1 text-body-sm text-fg shadow-md">
              {dragging.folder ? (
                <Folder className="size-3.5 shrink-0 text-fg-tertiary" aria-hidden />
              ) : (
                <FileText className="size-3.5 shrink-0 text-fg-tertiary" aria-hidden />
              )}
              <span className="truncate">{dragName(dragging)}</span>
            </span>
          )}
        </DragOverlay>
      </DndContext>
    </nav>
  )
}

/** A folder or scope root as a drop target; `accepts` is true while a valid drop hovers it. */
function useDropTarget(target: DropTarget, enabled: boolean) {
  const { active } = useDndContext()
  const droppable = useDroppable({
    id: `drop:${target.scope}:${target.prefix}`,
    data: target,
    disabled: !enabled,
  })
  const accepts = droppable.isOver && validDrop(active?.data.current as DragItem | undefined, target) !== null
  return { setNodeRef: droppable.setNodeRef, accepts }
}

function useDragItem(item: DragItem, enabled: boolean) {
  const draggable = useDraggable({
    id: `drag:${item.scope}:${item.folder ? 'folder' : 'file'}:${item.path}`,
    data: item,
    disabled: !enabled,
  })
  // Only the pointer listeners: dnd-kit's attributes would turn the row into a
  // role="button" and overwrite its own aria-disabled; the keyboard path is
  // the Rename or move dialog.
  return { setNodeRef: draggable.setNodeRef, listeners: draggable.listeners, isDragging: draggable.isDragging }
}

function ScopeRoot({
  slug,
  scope,
  label,
  hint,
  docs,
  treeEmpty,
  filter,
  active,
  actions,
}: {
  slug: string
  scope: DocScope
  label: string
  /** Whose notes these are, on hover over the heading. */
  hint: string
  docs: DocSummary[]
  /** Neither root holds a note: the page's empty state speaks for both. */
  treeEmpty: boolean
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
  const canDrag = Boolean(actions?.onDropMove && actions.canEditScope(scope))
  const { setNodeRef: rootRef, accepts: rootAccepts } = useDropTarget({ scope, prefix: '' }, canDrag)
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
  // Stable, so a folder's open-on-hover timer is not restarted by every render
  // a drag causes.
  const toggle = useCallback(
    (prefix: string) => {
      if (filtering) return
      setCollapsed(prev => {
        const next = new Set(prev)
        if (next.has(prefix)) next.delete(prefix)
        else next.add(prefix)
        return next
      })
    },
    [filtering],
  )

  const headingId = `docs-root-${scope}`
  return (
    <section
      ref={rootRef}
      aria-labelledby={headingId}
      className={cn('min-w-0 rounded-control', rootAccepts && 'bg-surface-active ring-1 ring-accent')}
    >
      <div className="flex items-center gap-1.5 px-1 pb-1">
        {scope === 'organization' ? (
          <Building2 className="size-3.5 shrink-0 text-fg-tertiary" aria-hidden />
        ) : (
          <FileText className="size-3.5 shrink-0 text-fg-tertiary" aria-hidden />
        )}
        <h2 id={headingId} title={hint} className="micro-label m-0 min-w-0 flex-1 truncate text-fg-tertiary">
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
        filtering || !treeEmpty ? (
          <p className="m-0 px-2 py-1 text-caption text-fg-tertiary">
            {filtering ? 'No match.' : 'No notes yet.'}
          </p>
        ) : null
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
          canDrag={canDrag}
          actions={actions}
        />
      )}
    </section>
  )
}

interface FolderListProps {
  slug: string
  scope: DocScope
  folder: DocTreeFolder<DocSummary>
  depth: number
  active: string | null
  isOpen: (prefix: string) => boolean
  toggle: (prefix: string) => void
  /** True while a filter is on: folders stay open so no match is hidden. */
  toggleLocked: boolean
  /** Whether rows can be dragged onto folders (an editor of this scope). */
  canDrag: boolean
  actions?: FolderActions
}

function FolderList(props: FolderListProps) {
  const { slug, scope, folder, depth, active, canDrag } = props
  return (
    <ul className="m-0 list-none p-0">
      {folder.folders.map(sub => (
        <FolderItem key={sub.path} {...props} sub={sub} />
      ))}
      {folder.files.map(file => (
        <FileItem
          key={file.path}
          slug={slug}
          scope={scope}
          path={file.path}
          name={file.name}
          doc={file.doc}
          depth={depth}
          selected={active !== null && file.path.toLowerCase() === active.toLowerCase()}
          canDrag={canDrag}
        />
      ))}
    </ul>
  )
}

/** How long a dragged item hovers a collapsed folder before it opens. */
const EXPAND_ON_HOVER_MS = 600

function FolderItem({ sub, ...props }: FolderListProps & { sub: DocTreeFolder<DocSummary> }) {
  const { scope, depth, isOpen, toggle, toggleLocked, canDrag, actions } = props
  const open = isOpen(sub.path)
  const indent = { paddingLeft: `${depth * 12 + 4}px` }
  // The whole <li> is the target, so dropping on a note inside a folder lands
  // in that folder.
  const { setNodeRef: dropRef, accepts } = useDropTarget({ scope, prefix: sub.path }, canDrag)
  const { setNodeRef: dragRef, listeners, isDragging } = useDragItem({ scope, path: sub.path, folder: true }, canDrag)
  const hoverToOpen = accepts && !open
  useEffect(() => {
    if (!hoverToOpen) return
    const timer = window.setTimeout(() => toggle(sub.path), EXPAND_ON_HOVER_MS)
    return () => window.clearTimeout(timer)
  }, [hoverToOpen, toggle, sub.path])

  return (
    <li ref={dropRef} className={cn('rounded-control', accepts && 'bg-surface-active ring-1 ring-accent')}>
      <div
        className={cn('group flex items-center rounded-control hover:bg-surface-hover', isDragging && 'opacity-50')}
        style={indent}
      >
        <button
          ref={dragRef}
          {...listeners}
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
      {open && <FolderList {...props} folder={sub} depth={depth + 1} />}
    </li>
  )
}

function FileItem({
  slug,
  scope,
  path,
  name,
  doc,
  depth,
  selected,
  canDrag,
}: {
  slug: string
  scope: DocScope
  path: string
  name: string
  doc: DocSummary
  depth: number
  selected: boolean
  canDrag: boolean
}) {
  const { setNodeRef, listeners, isDragging } = useDragItem({ scope, path, folder: false }, canDrag)
  return (
    <li>
      <Link
        ref={setNodeRef}
        {...listeners}
        // The browser's own link drag would take the pointer away from dnd-kit.
        draggable={false}
        to={docRoute(slug, scope, path)}
        aria-current={selected ? 'page' : undefined}
        title={path}
        aria-label={`${doc.title || name}${visibilitySuffix(doc)}`}
        className={cn(
          'flex min-w-0 items-center gap-1.5 rounded-control py-1 pr-1 text-body-sm hover:bg-surface-hover',
          selected ? 'bg-surface-active font-medium text-fg' : 'text-fg-secondary',
          isDragging && 'opacity-50',
        )}
        style={{ paddingLeft: `${depth * 12 + 20}px` }}
      >
        <FileText className="size-3.5 shrink-0 text-fg-tertiary" aria-hidden />
        <span className="truncate">{doc.title || name}</span>
        <VisibilityIcon doc={doc} />
      </Link>
    </li>
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
