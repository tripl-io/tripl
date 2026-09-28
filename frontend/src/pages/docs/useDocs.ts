import { useMemo } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { docsApi } from '@/api/docs'
import { useDebouncedValue } from '@/hooks/useDebouncedValue'
import { extractDocLinks, MAX_LINK_REFS } from '@/lib/docLinks'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import {
  docFileKey,
  docLinkResolutionKey,
  docRevisionKey,
  docRevisionsKey,
  docSharingKey,
  docsKey,
  docsTreeKey,
} from '@/lib/docsQueryKeys'
import type { DocMoveRequest, DocScope, DocSharingUpdate, DocWriteRequest } from '@/types/docs'

/** The live preview asks the server to resolve links this long after typing stops. */
export const LINK_PREVIEW_DEBOUNCE_MS = 400

export function useDocTree(slug: string) {
  return useQuery({
    queryKey: docsTreeKey(slug),
    queryFn: ({ signal }) => docsApi.tree(slug, signal),
    enabled: Boolean(slug),
  })
}

export function useDocFile(slug: string, scope: DocScope | null, path: string) {
  return useQuery({
    queryKey: docFileKey(slug, scope ?? 'project', path),
    queryFn: ({ signal }) => docsApi.read(slug, scope ?? 'project', path, signal),
    enabled: Boolean(slug && scope && path),
    // The page renders its own not-found and error states.
    meta: SILENT_ERROR_META,
    retry: false,
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

export { useInvalidateDocs }
