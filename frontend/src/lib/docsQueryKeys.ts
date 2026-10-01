import { orgRoot } from '@/lib/activeOrg'
import type { DocLinkKind, DocScope } from '@/types/docs'

/**
 * Query keys for the docs catalog (F22), built the way lib/queryKeys.ts
 * builds its families: every key extends {@link docsKey}, so one
 * `invalidateQueries({ queryKey: docsKey(slug) })` after a write refreshes the
 * tree, the open note, its history and every Notes card of the project.
 *
 * Kept beside queryKeys.ts rather than in it so the event page's Notes card
 * does not import the whole key module's API clients just for these.
 */
export const docsKey = (slug: string) => [...orgRoot(), 'docs', slug] as const

export const docsTreeKey = (slug: string) => [...docsKey(slug), 'tree'] as const

export const docFileKey = (slug: string, scope: DocScope, path: string, lang: string) =>
  [...docsKey(slug), 'file', scope, path, lang] as const

export const docTranslationRevisionsKey = (slug: string, scope: DocScope, path: string, lang: string) =>
  [...docsKey(slug), 'translation-revisions', scope, path, lang] as const

/** A note's (`target: 'file'`) or folder's sharing (F24). */
export const docSharingKey = (slug: string, target: 'file' | 'folder', scope: DocScope, path: string) =>
  [...docsKey(slug), 'sharing', target, scope, path] as const

export const docRevisionsKey = (slug: string, scope: DocScope, path: string) =>
  [...docsKey(slug), 'revisions', scope, path] as const

export const docRevisionKey = (slug: string, revisionId: string) =>
  [...docsKey(slug), 'revision', revisionId] as const

export const docBacklinksKey = (
  slug: string,
  kind: DocLinkKind,
  name: string,
  qualifier: string | null,
) => [...docsKey(slug), 'backlinks', kind, name, qualifier] as const

/** Live-preview link resolution; `refs` is the sorted `kind:target` list. */
export const docLinkResolutionKey = (slug: string, refs: readonly string[]) =>
  [...docsKey(slug), 'links', refs.join('\n')] as const

/** The editor's `[[` / `@` picker rows for one (debounced) query. */
export const docLinkSuggestionsKey = (slug: string, q: string, kind: DocLinkKind | null) =>
  [...docsKey(slug), 'link-suggestions', kind, q] as const

/** Full-text docs search (quick open's "In note text"). */
export const docSearchKey = (slug: string, q: string) => [...docsKey(slug), 'search', q] as const
