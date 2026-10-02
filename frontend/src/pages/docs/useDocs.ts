import { useMemo } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { docsApi } from '@/api/docs'
import { useDebouncedValue } from '@/hooks/useDebouncedValue'
import { extractDocLinks, MAX_LINK_REFS } from '@/lib/docLinks'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import {
  docFileKey,
  docLinkResolutionKey,
  docLinkSuggestionsKey,
  docRevisionKey,
  docRevisionsKey,
  docSharingKey,
  docsKey,
  docTranslationRevisionsKey,
  docsTreeKey,
} from '@/lib/docsQueryKeys'
import type {
  DocFileResponse,
  DocLinkKind,
  DocMoveRequest,
  DocScope,
  DocSharingUpdate,
  DocTranslateRequest,
  DocTranslationWrite,
  DocWriteRequest,
} from '@/types/docs'

/** The live preview asks the server to resolve links this long after typing stops. */
export const LINK_PREVIEW_DEBOUNCE_MS = 400

export function useDocTree(slug: string) {
  return useQuery({
    queryKey: docsTreeKey(slug),
    queryFn: ({ signal }) => docsApi.tree(slug, signal),
    enabled: Boolean(slug),
  })
}

/** While a translation is being made, the open note re-reads this often. */
export const TRANSLATION_POLL_MS = 3000

/**
 * One note in `lang` (a translation's code, or `original`); null waits until
 * the page knows which language to show. Re-reads while a translation of it
 * is being made, so the page sees it land.
 */
export function useDocFile(slug: string, scope: DocScope | null, path: string, lang: string | null) {
  return useQuery({
    queryKey: docFileKey(slug, scope ?? 'project', path, lang ?? ''),
    queryFn: ({ signal }) => docsApi.read(slug, scope ?? 'project', path, lang ?? 'original', signal),
    enabled: Boolean(slug && scope && path && lang),
    // The page renders its own not-found and error states.
    meta: SILENT_ERROR_META,
    retry: false,
    refetchInterval: query => translationPollInterval(query.state.data),
  })
}

/** How often to re-read a note: while a translation of it is being made, and never otherwise. */
export function translationPollInterval(doc: DocFileResponse | undefined): number | false {
  return doc?.translations?.some(t => t.status === 'pending') ? TRANSLATION_POLL_MS : false
}

export function useTranslationRevisions(slug: string, scope: DocScope, path: string, lang: string, enabled: boolean) {
  return useQuery({
    queryKey: docTranslationRevisionsKey(slug, scope, path, lang),
    queryFn: ({ signal }) => docsApi.translationRevisions(slug, scope, path, lang, signal),
    enabled: enabled && Boolean(slug && path && lang),
  })
}

export function useDocRevisions(slug: string, scope: DocScope, path: string, enabled: boolean) {
  return useQuery({
    queryKey: docRevisionsKey(slug, scope, path),
    queryFn: ({ signal }) => docsApi.revisions(slug, scope, path, signal),
    enabled: enabled && Boolean(slug && path),
  })
}

export function useDocRevision(slug: string, revisionId: string | null) {
  return useQuery({
    queryKey: docRevisionKey(slug, revisionId ?? ''),
    queryFn: ({ signal }) => docsApi.revision(slug, revisionId ?? '', signal),
    enabled: Boolean(slug && revisionId),
  })
}

/**
 * Resolve the `[[…]]` links of a draft for the editor preview, debounced so
 * typing does not fire a request per key. Returns the resolutions of the last
 * settled draft while the next one loads.
 */
export function useDraftLinkResolutions(slug: string, content: string) {
  const debounced = useDebouncedValue(content, LINK_PREVIEW_DEBOUNCE_MS)
  const refs = useMemo(
    () =>
      [...new Set(extractDocLinks(debounced).map(link => link.ref))]
        .sort()
        .slice(0, MAX_LINK_REFS),
    [debounced],
  )
  return useQuery({
    queryKey: docLinkResolutionKey(slug, refs),
    queryFn: ({ signal }) => docsApi.links(slug, refs, signal),
    enabled: Boolean(slug),
    placeholderData: keepPreviousData,
    meta: SILENT_ERROR_META,
    staleTime: 30_000,
  })
}

/** The editor's link picker waits this long after the last keystroke. */
export const LINK_SUGGEST_DEBOUNCE_MS = 200
/** How many rows the picker shows. */
export const LINK_SUGGEST_LIMIT = 8

/**
 * Rows for the editor's `[[` / `@` picker (F24), debounced so typing does not
 * fire a request per key; the last rows stay up while the next ones load.
 */
/**
 * The link picker's rows. `current` is true only when `data` answers exactly
 * the typed (kind, query): while the debounce runs or the next query loads,
 * `keepPreviousData` keeps the previous rows, which may be of another kind (the
 * `[[` rows after `@` or `[[metric:` is typed), and the picker must not offer
 * them.
 */
export function useLinkSuggestions(slug: string, query: string, kind: DocLinkKind | null, enabled: boolean) {
  const wanted = query.trim()
  const debounced = useDebouncedValue(wanted, LINK_SUGGEST_DEBOUNCE_MS)
  const result = useQuery({
    queryKey: docLinkSuggestionsKey(slug, debounced, kind),
    queryFn: ({ signal }) => docsApi.linkSuggestions(slug, { q: debounced, kind, limit: LINK_SUGGEST_LIMIT }, signal),
    enabled: enabled && Boolean(slug),
    placeholderData: keepPreviousData,
    // A failed lookup shows "No matches" in the picker, not a toast mid-typing.
    meta: SILENT_ERROR_META,
    staleTime: 30_000,
  })
  const current = result.data !== undefined && !result.isPlaceholderData && debounced === wanted
  return { data: result.data, isFetching: result.isFetching, current }
}

/** Every docs write refreshes the whole project's docs family. */
function useInvalidateDocs(slug: string) {
  const queryClient = useQueryClient()
  return () => queryClient.invalidateQueries({ queryKey: docsKey(slug) })
}

export function useWriteDoc(slug: string) {
  const invalidate = useInvalidateDocs(slug)
  return useMutation({
    mutationFn: (vars: { scope: DocScope; path: string; body: DocWriteRequest }) =>
      docsApi.write(slug, vars.scope, vars.path, vars.body),
    onSuccess: () => invalidate(),
    // The editor shows conflicts and validation errors in place.
    meta: SILENT_ERROR_META,
  })
}

export function useDeleteDoc(slug: string) {
  const invalidate = useInvalidateDocs(slug)
  return useMutation({
    mutationFn: (vars: { scope: DocScope; path: string }) => docsApi.remove(slug, vars.scope, vars.path),
    onSuccess: () => invalidate(),
    meta: SILENT_ERROR_META,
  })
}

export function useDeleteDocFolder(slug: string) {
  const invalidate = useInvalidateDocs(slug)
  return useMutation({
    mutationFn: (vars: { scope: DocScope; prefix: string }) =>
      docsApi.removeFolder(slug, vars.scope, vars.prefix),
    onSuccess: () => invalidate(),
    meta: SILENT_ERROR_META,
  })
}

export function useMoveDoc(slug: string) {
  const invalidate = useInvalidateDocs(slug)
  return useMutation({
    mutationFn: (body: DocMoveRequest) => docsApi.move(slug, body),
    onSuccess: () => invalidate(),
    meta: SILENT_ERROR_META,
  })
}

export function useRestoreDocRevision(slug: string) {
  const invalidate = useInvalidateDocs(slug)
  return useMutation({
    mutationFn: (vars: { revisionId: string; message?: string }) =>
      docsApi.restore(slug, vars.revisionId, vars.message ?? ''),
    onSuccess: () => invalidate(),
  })
}

/** What the Share dialog edits: one note, or one folder of a scope. */
export interface DocSharingTarget {
  kind: 'file' | 'folder'
  scope: DocScope
  /** The note's path, or the folder prefix (trailing slash) as the tree has it. */
  path: string
}

export function useDocSharing(slug: string, target: DocSharingTarget | null) {
  return useQuery({
    queryKey: docSharingKey(slug, target?.kind ?? 'file', target?.scope ?? 'project', target?.path ?? ''),
    queryFn: ({ signal }) => {
      const t = target as DocSharingTarget
      return t.kind === 'file'
        ? docsApi.fileSharing(slug, t.scope, t.path, signal)
        : docsApi.folderSharing(slug, t.scope, t.path, signal)
    },
    enabled: Boolean(slug && target),
    // The dialog shows its own error state.
    meta: SILENT_ERROR_META,
    retry: false,
  })
}

/**
 * Save a note's or folder's sharing. Refreshes the whole docs family: a
 * visibility change moves notes in and out of every list, count and search.
 */
export function useUpdateDocSharing(slug: string) {
  const invalidate = useInvalidateDocs(slug)
  return useMutation({
    mutationFn: (vars: { target: DocSharingTarget; body: DocSharingUpdate }) =>
      vars.target.kind === 'file'
        ? docsApi.updateFileSharing(slug, vars.target.scope, vars.target.path, vars.body)
        : docsApi.updateFolderSharing(slug, vars.target.scope, vars.target.path, vars.body),
    onSuccess: () => invalidate(),
    // The dialog shows a refusal (403) in place.
    meta: SILENT_ERROR_META,
  })
}

/** Ask for an AI translation; the dialog shows a refusal (409 edited, 422 language) itself. */
export function useTranslateDoc(slug: string) {
  const invalidate = useInvalidateDocs(slug)
  return useMutation({
    mutationFn: (body: DocTranslateRequest) => docsApi.translate(slug, body),
    onSuccess: () => invalidate(),
    meta: SILENT_ERROR_META,
  })
}

export function useWriteTranslation(slug: string) {
  const invalidate = useInvalidateDocs(slug)
  return useMutation({
    mutationFn: (body: DocTranslationWrite) => docsApi.writeTranslation(slug, body),
    onSuccess: () => invalidate(),
    // The editor shows conflicts and validation errors in place.
    meta: SILENT_ERROR_META,
  })
}

export function useRemoveTranslation(slug: string) {
  const invalidate = useInvalidateDocs(slug)
  return useMutation({
    mutationFn: (vars: { scope: DocScope; path: string; lang: string }) =>
      docsApi.removeTranslation(slug, vars.scope, vars.path, vars.lang),
    onSuccess: () => invalidate(),
    meta: SILENT_ERROR_META,
  })
}

export function useRestoreTranslationRevision(slug: string) {
  const invalidate = useInvalidateDocs(slug)
  return useMutation({
    mutationFn: (vars: { scope: DocScope; path: string; revisionId: string }) =>
      docsApi.restoreTranslationRevision(slug, vars.scope, vars.path, vars.revisionId),
    onSuccess: () => invalidate(),
  })
}

export function useUpdateDocLanguages(slug: string) {
  const invalidate = useInvalidateDocs(slug)
  return useMutation({
    mutationFn: (body: { agent_lang: string | null; human_lang: string | null }) =>
      docsApi.updateLanguages(slug, body),
    onSuccess: () => invalidate(),
    // The dialog shows a refusal (a name the model could not place) in place.
    meta: SILENT_ERROR_META,
  })
}

export { useInvalidateDocs }
