import type { DocScope } from '@/types/docs'
import { currentOrgSlug, projectPath } from '@/lib/activeOrg'

/**
 * Pure helpers for the docs catalog (F22): folder trees built from flat paths,
 * doc routes, client-side path checks and a small fuzzy matcher.
 *
 * Folders are IMPLICIT on the server — a folder exists while some note's path
 * runs through it — so the tree is derived here from the list the API returns.
 */

/** Mirrors `services/docs_paths.py`: shown before the server says so. */
export const DOC_PATH_MAX_CHARS = 512
export const DOC_PATH_MAX_SEGMENTS = 10
const SEGMENT_RE = /^[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,127}$/

export interface DocTreeFile<T> {
  kind: 'file'
  name: string
  path: string
  doc: T
}

export interface DocTreeFolder<T> {
  kind: 'folder'
  name: string
  /** The folder prefix WITH its trailing slash (`guides/`); `''` at the root. */
  path: string
  folders: DocTreeFolder<T>[]
  files: DocTreeFile<T>[]
  /** Notes anywhere below this folder. */
  count: number
}

const collator = new Intl.Collator(undefined, { sensitivity: 'base', numeric: true })

/**
 * Build a folder tree from notes keyed by path. Folders sort before files,
 * both case-insensitively; a `SKILL.md` or `README.md` sorts first among its
 * siblings, because that is the note a reader opens a folder for.
 */
export function buildDocTree<T extends { path: string }>(docs: readonly T[]): DocTreeFolder<T> {
  const root: DocTreeFolder<T> = { kind: 'folder', name: '', path: '', folders: [], files: [], count: 0 }
  for (const doc of docs) {
    const segments = doc.path.split('/')
    let folder = root
    folder.count += 1
    for (let i = 0; i < segments.length - 1; i += 1) {
      const name = segments[i] ?? ''
      const path = `${segments.slice(0, i + 1).join('/')}/`
      const found = folder.folders.find(candidate => candidate.path === path)
      const next: DocTreeFolder<T> = found ?? { kind: 'folder', name, path, folders: [], files: [], count: 0 }
      if (!found) folder.folders.push(next)
      next.count += 1
      folder = next
    }
    folder.files.push({ kind: 'file', name: segments[segments.length - 1] ?? doc.path, path: doc.path, doc })
  }
  sortFolder(root)
  return root
}

const LEAD_FILES = new Set(['skill.md', 'readme.md', 'index.md'])

function sortFolder<T>(folder: DocTreeFolder<T>): void {
  folder.folders.sort((a, b) => collator.compare(a.name, b.name))
  folder.files.sort((a, b) => {
    const leadA = LEAD_FILES.has(a.name.toLowerCase()) ? 0 : 1
    const leadB = LEAD_FILES.has(b.name.toLowerCase()) ? 0 : 1
    return leadA - leadB || collator.compare(a.name, b.name)
  })
  folder.folders.forEach(sortFolder)
}

/** Every folder prefix a path runs through: `a/b/c.md` → `['a/', 'a/b/']`. */
export function ancestorFolders(path: string): string[] {
  const segments = path.split('/')
  const out: string[] = []
  for (let i = 1; i < segments.length; i += 1) out.push(`${segments.slice(0, i).join('/')}/`)
  return out
}

/** The folder prefix of a path, `''` at the root. */
export function folderOf(path: string): string {
  const cut = path.lastIndexOf('/')
  return cut < 0 ? '' : path.slice(0, cut + 1)
}

export function baseName(path: string): string {
  const cut = path.lastIndexOf('/')
  return cut < 0 ? path : path.slice(cut + 1)
}

/** Whether `path` sits under the folder `prefix` (with trailing slash). */
export function isUnder(path: string, prefix: string): boolean {
  return prefix === '' || path.toLowerCase().startsWith(prefix.toLowerCase())
}

/**
 * Where a note or folder lands when dropped on the folder `target` (`''` is
 * the scope root): the target plus the dragged item's own name. `null` when
 * the drop moves nothing (its own folder) or cannot happen (a folder onto
 * itself or one of its subfolders).
 */
export function dropPath(from: string, folder: boolean, target: string): string | null {
  const name = folder ? `${baseName(from.slice(0, -1))}/` : baseName(from)
  const to = `${target}${name}`
  if (to.toLowerCase() === from.toLowerCase()) return null
  if (folder && target !== '' && target.toLowerCase().startsWith(from.toLowerCase())) return null
  return to
}

/**
 * Whether the notes under `prefix` are exactly `paths` (case-insensitive);
 * false with no notes to look at. Undoing a folder drop is only safe then.
 */
export function folderHoldsOnly(docs: readonly { path: string }[] | undefined, prefix: string, paths: readonly string[]): boolean {
  if (!docs) return false
  const under = docs.filter(doc => isUnder(doc.path, prefix)).map(doc => doc.path.toLowerCase())
  const expected = new Set(paths.map(path => path.toLowerCase()))
  return under.length === expected.size && under.every(path => expected.has(path))
}

/** The in-app address of a note; each segment is encoded, slashes kept. */
export function docRoute(slug: string, scope: DocScope, path: string): string {
  const encoded = path.split('/').map(encodeURIComponent).join('/')
  return projectPath(currentOrgSlug(), slug, `/docs/${scope}/${encoded}`)
}

export function isDocScope(value: string | undefined): value is DocScope {
  return value === 'project' || value === 'organization'
}

/**
 * The note path from the route splat. React Router hands params back already
 * decoded, so decoding again would turn a literal `%25` in a name into `%`.
 */
export function docPathFromSplat(splat: string | undefined): string {
  return (splat ?? '').replace(/^\/+|\/+$/g, '')
}

export type PathCheck = { ok: true; path: string } | { ok: false; error: string }

/**
 * Normalise and check a path typed into a dialog. The server re-validates;
 * this only answers early. `.md` is appended when missing (`asFolder` checks
 * a folder prefix instead and returns it with a trailing slash).
 */
export function checkDocPath(raw: string, { asFolder = false } = {}): PathCheck {
  let path = raw.normalize('NFC').trim()
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(path)) return { ok: false, error: 'Control characters are not allowed.' }
  if (path.includes('\\')) return { ok: false, error: 'Use "/" to separate folders, not "\\".' }
  path = path.replace(/\/{2,}/g, '/')
  while (path.startsWith('./')) path = path.slice(2)
  if (path.startsWith('/')) return { ok: false, error: 'Paths are relative: drop the leading "/".' }
  if (asFolder) path = path.replace(/\/+$/, '')
  if (!path) return { ok: false, error: asFolder ? 'Enter a folder name.' : 'Enter a file name.' }
  if (!asFolder && !/\.md$/i.test(path)) path = `${path}.md`
  const segments = path.split('/')
  if (segments.length > DOC_PATH_MAX_SEGMENTS) {
    return { ok: false, error: `At most ${DOC_PATH_MAX_SEGMENTS} folder levels.` }
  }
  for (const segment of segments) {
    if (segment === '') return { ok: false, error: 'Empty folder names are not allowed.' }
    if (segment === '.' || segment === '..') return { ok: false, error: '"." and ".." are not allowed.' }
    if (!SEGMENT_RE.test(segment)) {
      return {
        ok: false,
        error: `"${segment}" must start with a letter or digit and use only letters, digits, spaces and . _ ( ) + -`,
      }
    }
  }
  const full = asFolder ? `${path}/` : path
  if (full.length > DOC_PATH_MAX_CHARS) return { ok: false, error: `At most ${DOC_PATH_MAX_CHARS} characters.` }
  return { ok: true, path: full }
}

/**
 * A subsequence fuzzy score: every query character must appear in order.
 * Higher is better; `null` means no match. Word starts, consecutive runs and
 * an early first hit score more, so `ckout` finds `checkout.md` before
 * `docs/back-of-house/ticket-output.md`.
 */
export function fuzzyScore(query: string, text: string): number | null {
  const q = query.trim().toLowerCase()
  if (!q) return 0
  const t = text.toLowerCase()
  let score = 0
  let ti = 0
  let run = 0
  let first = -1
  for (const ch of q) {
    if (ch === ' ') continue
    const found = t.indexOf(ch, ti)
    if (found < 0) return null
    if (first < 0) first = found
    const prev = found > 0 ? t.charAt(found - 1) : '/'
    const boundary = /[/_\-. ]/.test(prev)
    run = found === ti && ti > 0 ? run + 1 : 0
    score += 1 + (boundary ? 3 : 0) + run * 2
    ti = found + 1
  }
  return score - Math.min(first, 20) * 0.1 - t.length * 0.01
}

/** Best fuzzy score over a note's title and path. */
export function docMatchScore(query: string, doc: { title: string; path: string }): number | null {
  const byTitle = fuzzyScore(query, doc.title)
  const byPath = fuzzyScore(query, doc.path)
  if (byTitle === null) return byPath
  if (byPath === null) return byTitle + 1
  return Math.max(byTitle + 1, byPath)
}

/** Title for a new note from its path's file stem. */
export function titleFromPath(path: string): string {
  const stem = baseName(path).replace(/\.md$/i, '')
  const words = stem.replace(/[-_]+/g, ' ').trim()
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : 'Untitled'
}

/** The starting content of a new note: frontmatter plus a heading. */
export function newDocTemplate(path: string, { body = '' }: { body?: string } = {}): string {
  const title = titleFromPath(path)
  const lines = ['---', `title: ${JSON.stringify(title)}`, 'description: ""', 'tags: []', 'audience: both', '---', '', `# ${title}`, '']
  if (body) lines.push(body, '')
  return lines.join('\n')
}

/** `1234` → `1.2 KB`. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
