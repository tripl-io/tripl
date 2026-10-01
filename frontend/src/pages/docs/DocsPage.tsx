import { Suspense, useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'
import { ArrowDownUp, FilePlus2, Languages, NotebookText, Search } from 'lucide-react'
import { ErrorState } from '@/components/error-state'
import { EmptyState } from '@/components/empty-state'
import { Kbd } from '@/components/primitives/kbd'
import { PageContainer } from '@/components/primitives/page-container'
import { PageHeader } from '@/components/primitives/page-header'
import { EntityNotFound, isNotFoundError, PageSkeleton, SectionSkeleton } from '@/components/states'
import { usePageTitle } from '@/components/shell-chrome-context'
import { Button } from '@/components/ui/button'
import { useConfirm } from '@/hooks/useConfirm'
import { ORIGINAL_LANG, storeDocLanguage, storedDocLanguage } from '@/lib/docLanguages'
import { docPathFromSplat, docRoute, isDocScope, isUnder } from '@/lib/docTree'
import { formatRelativeTime } from '@/lib/datetime'
import { lazyWithReload } from '@/lib/lazyWithReload'
import { useCanWriteProject, useIsOwner } from '@/lib/permissions'
import type { DocScope, DocSummary, DocTreeResponse } from '@/types/docs'
import { MoveDocDialog, NewDocDialog, type MoveRequest, type NewDocRequest } from './DocFileDialogs'
import { DocHistoryPanel } from './DocHistoryPanel'
import { DocImportExportDialog } from './DocImportExportDialog'
import { DocShareDialog } from './DocShareDialog'
import { DocQuickOpen } from './DocQuickOpen'
import { DocsTree } from './DocsTree'
import { DocLanguageBar, DocLanguagesDialog, DocTranslationNotice, TranslationHistoryDialog } from './DocTranslations'
import { DocView } from './DocView'
import { useDeleteDoc, useDeleteDocFolder, useDocFile, useDocTree, type DocSharingTarget } from './useDocs'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

// The editor carries CodeMirror and its Markdown grammar; readers never load it.
const DocEditor = lazyWithReload(() => import('./DocEditor').then(m => ({ default: m.DocEditor })))

/**
 * The docs catalog (F22, GH #299): Markdown notes for people and AI agents,
 * the project's own plus its organization's, in one tree. Not branch-aware —
 * one version of each note — and `[[event:…]]` links resolve against main.
 *
 * Routes: `/p/:slug/docs` (the index) and `/p/:slug/docs/:scope/*` (a note;
 * the splat is its path). `?edit=1` opens the editor; `?new=1&link=…` opens
 * the new-note dialog pre-filled (the Notes card's "New note about this").
 */
export default function DocsPage() {
  const { slug = '', scope: scopeParam, '*': splat } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const canEdit = useCanWriteProject()
  // An organization owner or admin (useIsOwner covers both since F20 PR4): the
  // only writers of organization notes (services/docs_access.py).
  const isOrgAdmin = useIsOwner()
  const canEditScope = (s: DocScope | null) => canEdit && (s === 'project' || (s === 'organization' && isOrgAdmin))
  const tree = useDocTree(slug)
  const scope: DocScope | null = isDocScope(scopeParam) ? scopeParam : null
  const path = docPathFromSplat(splat)
  // Which language to show: the link's `?lang`, else this browser's last
  // choice for the project, else the project's default for people. Null until
  // the tree says what that default is.
  const [storedLang, setStoredLang] = useState(() => storedDocLanguage(slug))
  const lang =
    searchParams.get('lang') ?? storedLang ?? (tree.data ? (tree.data.language_defaults?.human_lang ?? ORIGINAL_LANG) : null)
  const file = useDocFile(slug, scope, path, lang)
  const servedTranslation = file.data?.lang
    ? file.data.translations?.find(t => t.lang === file.data?.lang)
    : undefined
  // F24: the server also says what the caller may do with this note — a note
  // shared with them view-only, or someone else's private note opened by an
  // organization admin (break-glass, audited), is read-only.
  const canEditNote = canEditScope(scope) && file.data?.my_permission === 'edit'
  const editing = canEditNote && searchParams.get('edit') === '1'

  const [historyOpen, setHistoryOpen] = useState(false)
  const [languagesOpen, setLanguagesOpen] = useState(false)
  const [quickOpen, setQuickOpen] = useState(false)
  const [transferOpen, setTransferOpen] = useState(false)
  const [shareTarget, setShareTarget] = useState<DocSharingTarget | null>(null)
  const [newDoc, setNewDoc] = useState<NewDocRequest | null>(null)
  const [moveReq, setMoveReq] = useState<MoveRequest | null>(null)
  const { confirm, dialog: confirmDialog } = useConfirm()
  const deleteDoc = useDeleteDoc(slug)
  const deleteFolder = useDeleteDocFolder(slug)

  usePageTitle(file.data?.title ?? null)

  const setEditing = useCallback(
    (on: boolean) =>
      setSearchParams(
        prev => {
          const next = new URLSearchParams(prev)
          if (on) next.set('edit', '1')
          else next.delete('edit')
          return next
        },
        { replace: true },
      ),
    [setSearchParams],
  )

  const changeLang = useCallback(
    (next: string) => {
      storeDocLanguage(slug, next)
      setStoredLang(next)
      setSearchParams(
        prev => {
          const params = new URLSearchParams(prev)
          params.set('lang', next)
          return params
        },
        { replace: true },
      )
    },
    [slug, setSearchParams],
  )

  // "New note about this" from an event or event-type page arrives as
  // `?new=1&link=[[…]]`: derived from the URL, cleared when the dialog closes.
  const linkedNewDoc: NewDocRequest | null =
    canEdit && searchParams.get('new') === '1'
      ? { scope: 'project', folder: '', body: searchParams.get('link') ?? undefined }
      : null
  const closeNewDoc = () => {
    setNewDoc(null)
    if (linkedNewDoc) {
      setSearchParams(
        prev => {
          const next = new URLSearchParams(prev)
          next.delete('new')
          next.delete('link')
          return next
        },
        { replace: true },
      )
    }
  }

  // Ctrl/⌘+P: quick open (instead of the browser's print dialog).
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && !event.shiftKey && event.key.toLowerCase() === 'p') {
        event.preventDefault()
        setQuickOpen(true)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  if (tree.isPending) return <PageSkeleton variant="detail" label="Loading docs…" />
  if (tree.isError) {
    return (
      <PageContainer>
        <ErrorState title="Couldn't load the docs" error={tree.error} onRetry={() => void tree.refetch()} />
      </PageContainer>
    )
  }
  const data = tree.data
  const docsIndex = projectPath(currentOrgSlug(), slug, '/docs')

  const onDeleteNote = async () => {
    if (!scope || !path) return
    const ok = await confirm({
      title: 'Delete this note?',
      message: `${path} and its whole revision history are deleted. The audit log keeps a record of it.`,
      confirmLabel: 'Delete note',
      action: () => deleteDoc.mutateAsync({ scope, path }),
      errorPrefix: 'Could not delete the note',
      pendingLabel: 'Deleting…',
    })
    if (ok) {
      toast.success(`Deleted ${path}`)
      navigate(docsIndex)
    }
  }

  const onDeleteFolder = async (folderScope: DocScope, prefix: string, count: number) => {
    const ok = await confirm({
      title: `Delete ${prefix}?`,
      message: `All ${count} ${count === 1 ? 'note' : 'notes'} under this folder are deleted with their history.`,
      confirmLabel: 'Delete folder',
      requireText: count > 5 ? prefix : undefined,
      action: () => deleteFolder.mutateAsync({ scope: folderScope, prefix }),
      errorPrefix: 'Could not delete the folder',
      pendingLabel: 'Deleting…',
    })
    if (ok) {
      toast.success(`Deleted ${prefix}`)
      if (scope === folderScope && isUnder(path, prefix)) navigate(docsIndex)
    }
  }

  const folderActions = canEdit
    ? {
        onNewInFolder: (s: DocScope, prefix: string) => setNewDoc({ scope: s, folder: prefix }),
        onMoveFolder: (s: DocScope, prefix: string) => setMoveReq({ scope: s, from: prefix, folder: true }),
        onDeleteFolder: (s: DocScope, prefix: string, count: number) => void onDeleteFolder(s, prefix, count),
        onShareFolder: (s: DocScope, prefix: string) => setShareTarget({ kind: 'folder', scope: s, path: prefix }),
        canEditScope,
      }
    : undefined

  const total = data.project_docs.length + data.organization_docs.length

  return (
    <PageContainer>
      <PageHeader
        eyebrow="Plan"
        title="Docs"
        count={total}
        description={
          <>
            Notes for people and agents: this project's and {data.organization.name}'s. Link plan entities with{' '}
            <span className="mono">[[event:name]]</span>, <span className="mono">[[event-type:name]]</span> or{' '}
            <span className="mono">[[field:type/name]]</span>.
          </>
        }
        actions={
          <>
            <Button variant="outline" size="sm" onClick={() => setQuickOpen(true)} aria-keyshortcuts="Control+P Meta+P">
              <Search aria-hidden />
              Open note
              <Kbd className="ml-1">Ctrl P</Kbd>
            </Button>
            <Button variant="outline" size="sm" onClick={() => setLanguagesOpen(true)}>
              <Languages aria-hidden />
              Languages
            </Button>
            <Button variant="outline" size="sm" onClick={() => setTransferOpen(true)}>
              <ArrowDownUp aria-hidden />
              {canEdit ? 'Import / export' : 'Export'}
            </Button>
            {canEdit && (
              <Button size="sm" onClick={() => setNewDoc({ scope: scope ?? 'project', folder: '' })}>
                <FilePlus2 aria-hidden />
                New note
              </Button>
            )}
          </>
        }
      />

      <div className="grid min-w-0 items-start gap-6 lg:grid-cols-[260px_minmax(0,1fr)]">
        <aside className="min-w-0 lg:sticky lg:top-4 lg:max-h-[calc(100vh-6rem)] lg:overflow-y-auto">
          <DocsTree slug={slug} tree={data} active={scope && path ? { scope, path } : null} actions={folderActions} />
        </aside>
        <main className="min-w-0">
          {scopeParam !== undefined && !scope ? (
            <EntityNotFound title="Unknown notes scope" description="Notes live under /docs/project/… or /docs/organization/…." back={{ to: docsIndex, label: 'Back to docs' }} />
          ) : !scope || !path ? (
            <DocsIndex slug={slug} tree={data} canEdit={canEdit} onNew={() => setNewDoc({ scope: 'project', folder: '' })} onImport={() => setTransferOpen(true)} />
          ) : file.isPending ? (
            <SectionSkeleton label="Loading note…" />
          ) : file.isError ? (
            isNotFoundError(file.error) ? (
              <EntityNotFound
                title="Note not found"
                description={`There is no ${scope === 'organization' ? 'organization' : 'project'} note at ${path}. It may have been moved or deleted.`}
                back={{ to: docsIndex, label: 'Back to docs' }}
              />
            ) : (
              <ErrorState compact title="Couldn't load this note" error={file.error} onRetry={() => void file.refetch()} />
            )
          ) : editing ? (
            <Suspense fallback={<SectionSkeleton label="Loading editor…" />}>
              <DocEditor
                key={`${file.data.scope}:${file.data.path}:${file.data.lang ?? ORIGINAL_LANG}`}
                slug={slug}
                doc={file.data}
                // A translation may run up to three times the original's bytes
                // (two- and three-byte scripts); the server holds the same line.
                maxBytes={data.limits.max_file_bytes * (servedTranslation ? 3 : 1)}
                translation={servedTranslation ? { lang: servedTranslation.lang, revision: servedTranslation.revision } : null}
                onDone={() => setEditing(false)}
              />
            </Suspense>
          ) : (
            <DocView
              slug={slug}
              doc={file.data}
              canEdit={canEditNote}
              onEdit={() => setEditing(true)}
              onShare={() => setShareTarget({ kind: 'file', scope: file.data.scope, path: file.data.path })}
              onHistory={() => setHistoryOpen(true)}
              onMove={() => setMoveReq({ scope: file.data.scope, from: file.data.path, folder: false })}
              onDelete={() => void onDeleteNote()}
              languages={
                <>
                  <DocLanguageBar
                    slug={slug}
                    doc={file.data}
                    lang={lang ?? ORIGINAL_LANG}
                    canEdit={canEditNote}
                    onChangeLang={changeLang}
                  />
                  <DocTranslationNotice slug={slug} doc={file.data} canEdit={canEditNote} onChangeLang={changeLang} />
                </>
              }
            />
          )}
        </main>
      </div>

      {scope && path && !servedTranslation && (
        <DocHistoryPanel slug={slug} scope={scope} path={path} open={historyOpen} onOpenChange={setHistoryOpen} canEdit={canEditNote} />
      )}
      {scope && path && servedTranslation && (
        <TranslationHistoryDialog
          slug={slug}
          scope={scope}
          path={path}
          lang={servedTranslation.lang}
          open={historyOpen}
          onOpenChange={setHistoryOpen}
          canEdit={canEditNote}
        />
      )}
      <DocLanguagesDialog
        slug={slug}
        defaults={data.language_defaults ?? { agent_lang: null, human_lang: null }}
        open={languagesOpen}
        onOpenChange={setLanguagesOpen}
        canEdit={canEditScope('project')}
      />
      <DocShareDialog
        slug={slug}
        target={shareTarget}
        organizationSlug={data.organization.slug}
        organizationName={data.organization.name}
        // A guess until the server answers `can_manage`: an organization owner or
        // admin, or (for a note) someone who may edit it — the author always may.
        canManage={
          isOrgAdmin ||
          (shareTarget?.kind === 'file' ? file.data?.my_permission === 'edit' : canEditScope(shareTarget?.scope ?? null))
        }
        onClose={() => setShareTarget(null)}
      />
      <DocQuickOpen
        slug={slug}
        open={quickOpen}
        onOpenChange={setQuickOpen}
        projectDocs={data.project_docs}
        organizationDocs={data.organization_docs}
      />
      <DocImportExportDialog
        slug={slug}
        open={transferOpen}
        onOpenChange={setTransferOpen}
        canEdit={canEdit}
        isOrgAdmin={isOrgAdmin}
        organizationName={data.organization.name}
        limits={data.limits}
      />
      <NewDocDialog
        slug={slug}
        request={newDoc ?? linkedNewDoc}
        organizationName={data.organization.name}
        canWriteOrganization={isOrgAdmin}
        onClose={closeNewDoc}
        onCreated={created => {
          setNewDoc(null)
          navigate(`${docRoute(slug, created.scope, created.path)}?edit=1`)
        }}
      />
      <MoveDocDialog
        slug={slug}
        request={moveReq}
        onClose={() => setMoveReq(null)}
        onMoved={(moved, req) => {
          setMoveReq(null)
          toast.success(moved.length === 1 ? `Moved to ${moved[0]?.to_path}` : `Moved ${moved.length} notes`)
          const mine = scope === req.scope ? moved.find(m => m.from_path.toLowerCase() === path.toLowerCase()) : undefined
          if (mine) navigate(docRoute(slug, req.scope, mine.to_path), { replace: true })
        }}
      />
      {confirmDialog}
    </PageContainer>
  )
}

/** The landing view: what is there, most recently updated first. */
function DocsIndex({
  slug,
  tree,
  canEdit,
  onNew,
  onImport,
}: {
  slug: string
  tree: DocTreeResponse
  canEdit: boolean
  onNew: () => void
  onImport: () => void
}) {
  const all: DocSummary[] = [...tree.project_docs, ...tree.organization_docs]
  if (all.length === 0) {
    return (
      <EmptyState
        icon={NotebookText}
        title="No notes yet"
        description="Write down what the plan cannot say: warehouse gotchas, event query recipes, how a funnel is meant to be read. Agents read these notes too, through MCP and the CLI."
        action={
          canEdit ? (
            <div className="flex flex-wrap justify-center gap-2">
              <Button size="sm" onClick={onNew}>
                <FilePlus2 aria-hidden />
                New note
              </Button>
              <Button size="sm" variant="outline" onClick={onImport}>
                Import a folder
              </Button>
            </div>
          ) : undefined
        }
      />
    )
  }
  const recent = [...all].sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, 12)
  return (
    <section aria-labelledby="docs-recent" className="flex flex-col gap-2">
      <h2 id="docs-recent" className="micro-label m-0 text-fg-tertiary">
        Recently updated
      </h2>
      <ul className="m-0 flex list-none flex-col divide-y divide-border-subtle rounded-card border border-border p-0">
        {recent.map(doc => (
          <li key={`${doc.scope}:${doc.path}`} className="px-4 py-2.5">
            <Link to={docRoute(slug, doc.scope, doc.path)} className="font-medium text-fg hover:text-[var(--accent)] hover:underline">
              {doc.title}
            </Link>
            <div className="mono mt-0.5 truncate text-caption text-fg-tertiary">
              {doc.scope === 'organization' ? `${tree.organization.name} · ` : ''}
              {doc.path} · {formatRelativeTime(doc.updated_at)}
              {doc.updated_by_name ? ` · ${doc.updated_by_name}` : ''}
            </div>
            {doc.description && <p className="m-0 mt-0.5 line-clamp-2 text-body-sm text-fg-secondary">{doc.description}</p>}
          </li>
        ))}
      </ul>
    </section>
  )
}
