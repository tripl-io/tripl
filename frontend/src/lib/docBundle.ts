import type { DocBundleFile } from '@/types/docs'

/** Read an uploaded JSON bundle; only its files matter to the import. */
export function parseBundleFile(text: string): DocBundleFile[] {
  const parsed: unknown = JSON.parse(text)
  if (!parsed || typeof parsed !== 'object' || !Array.isArray((parsed as { files?: unknown }).files)) {
    throw new Error('Not a tripl docs bundle: expected a JSON object with a "files" list.')
  }
  const files = (parsed as { files: unknown[] }).files
  return files.map((file, i) => {
    const row = file as { path?: unknown; content?: unknown; sha256?: unknown }
    if (typeof row?.path !== 'string' || typeof row.content !== 'string') {
      throw new Error(`Entry ${i + 1} needs a string "path" and "content".`)
    }
    return { path: row.path, content: row.content, sha256: typeof row.sha256 === 'string' ? row.sha256 : null }
  })
}

